"""Страница отбора вакансий в браузере: `python -m snpr web`.

Локальный сервер только на 127.0.0.1 (снаружи не виден). Случайный токен
на запуск: он в адресе страницы и обязателен в заголовке X-Token каждого
/api/* — чужой сайт в браузере не сможет дёрнуть отклик. Держит тот же
data/approve.lock, что и консольный approve: одновременно работает что-то
одно. Страница закрыта (нет ping ~3 мин) — сервер завершается сам.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .approve_service import ApproveService, score_of

WEB_URL_FILE = "web_url.txt"
IDLE_TIMEOUT = 180.0  # сек без ping от страницы — выключаемся
STARTUP_GRACE = 300.0  # браузеру даём время открыться
MAX_BODY = 200_000


def approve_ui(config_path: Path | None = None) -> str:
    """config.toml [approve].ui: "web" (по умолчанию) или "console"."""
    import tomllib

    path = config_path or Path(__file__).with_name("config.toml")
    try:
        with path.open("rb") as fh:
            value = tomllib.load(fh).get("approve", {}).get("ui", "web")
    except (OSError, ValueError):
        value = "web"
    return "console" if str(value).strip().lower() == "console" else "web"


def _pub_ts(published_at: object) -> int:
    """Момент публикации вакансии (epoch-секунды) для сортировки; 0 — неизвестно."""
    from datetime import datetime, timedelta, timezone

    try:
        moment = datetime.fromisoformat(str(published_at or ""))
    except ValueError:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone(timedelta(hours=3)))
    return int(moment.timestamp())


def queue_items(service: ApproveService) -> list[dict[str, Any]]:
    from .approve import vacancy_age_hours
    from .cover_letter import clean_letter
    from .sources import source_of
    from .report import vacancy_fields

    items: list[dict[str, Any]] = []
    for rec in service.records():
        vacancy = rec.get("vacancy") or {}
        score = score_of(rec)
        # письма из очереди могли быть написаны до правки стиля — чистим и их
        letter = service.letter_of(rec)
        age = vacancy_age_hours(vacancy.get("published_at"))
        item = {
            "vid": str(vacancy.get("id") or ""),
            "name": str(vacancy.get("name") or ""),
            "employer": str((vacancy.get("employer") or {}).get("name") or ""),
            "score": score.score if score else None,
            "verdict": score.verdict if score else "",
            "reasons": score.reasons if score else [],
            "matched": score.matched if score else [],
            "missing": score.missing if score else [],
            "letter": letter,
            "violations": service.violations(letter, vacancy),
            "requests": score.requests if score else [],
            "resume_id": service.resume_for(rec),
            "age_hours": round(age, 1) if age is not None else None,
            "pub_ts": _pub_ts(vacancy.get("published_at")),
            "source": source_of(vacancy),
            "flags": list(vacancy.get("flags") or []),
        }
        item.update(vacancy_fields(vacancy))
        items.append(item)
    items.sort(key=lambda it: -(it["score"] if isinstance(it["score"], int) else -1))
    return items


def rejected_items(service: ApproveService) -> dict[str, Any]:
    """Вкладка «Не прошли порог»: вакансии со скором ниже maybe_min."""
    from datetime import datetime

    from .sources import source_of

    items = []
    for row in service.rejected():
        ts = int(row.get("ts") or 0)
        items.append(
            {
                "vid": str(row.get("vacancy_id") or ""),
                "name": str(row.get("name") or ""),
                "employer": str(row.get("employer") or ""),
                "url": str(row.get("url") or ""),
                "score": row.get("score"),
                "reasons": [str(r) for r in row.get("reasons") or []],
                "matched": [str(r) for r in row.get("matched") or []],
                "missing": [str(r) for r in row.get("missing") or []],
                "salary": str(row.get("salary") or ""),
                "area": str(row.get("area") or ""),
                "work_format": str(row.get("work_format") or ""),
                "date": datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "",
                "ts": ts,
                "pub_ts": _pub_ts(row.get("published_at")),
                "source": source_of(str(row.get("vacancy_id") or "")),
            }
        )
    return {"items": items, "threshold": service.threshold()}


class WebState:
    def __init__(self, service: ApproveService, token: str):
        self.service = service
        self.token = token
        self.last_ping = time.monotonic() + STARTUP_GRACE - IDLE_TIMEOUT
        self.allowed_hosts: set[str] = set()


def _make_handler(state: WebState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "snpr-web"

        def log_message(self, fmt: str, *args: Any) -> None:  # тихо
            pass

        # ------------------------------------------------------- helpers --
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: Any) -> None:
            self._send(
                code,
                json.dumps(data, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in state.allowed_hosts

        def _api_ok(self) -> bool:
            return self._host_ok() and secrets.compare_digest(
                self.headers.get("X-Token", ""), state.token
            )

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        # ---------------------------------------------------------- routes --
        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            if parts.path == "/":
                token = (parse_qs(parts.query).get("t") or [""])[0]
                if not self._host_ok() or not secrets.compare_digest(token, state.token):
                    self._send(403, "Нет доступа".encode("utf-8"), "text/plain; charset=utf-8")
                    return
                page = PAGE.replace("__TOKEN__", state.token).replace(
                    "__DRY__", "true" if state.service.dry_run else "false"
                )
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parts.path == "/api/responses":
                if not self._api_ok():
                    self._json(403, {"error": "forbidden"})
                    return
                vid = (parse_qs(parts.query).get("vid") or [""])[0]
                from .responses import site_blocked

                count = state.service.responses(vid) if vid.isdigit() else None
                self._json(200, {"vid": vid, "count": count, "blocked": site_blocked()})
                return
            if parts.path == "/api/rejected":
                if not self._api_ok():
                    self._json(403, {"error": "forbidden"})
                    return
                state.last_ping = time.monotonic()
                self._json(200, rejected_items(state.service))
                return
            if parts.path == "/api/queue":
                if not self._api_ok():
                    self._json(403, {"error": "forbidden"})
                    return
                state.last_ping = time.monotonic()
                # откликнулись на сайте, пока страница открыта — убрать
                try:
                    state.service.refresh_applied()
                except Exception as ex:  # noqa: BLE001 — сеть: показываем как есть
                    print(f"[отклики] сверка не удалась: {ex}")
                with state.service.lock:
                    items = [
                        it for it in queue_items(state.service)
                        if it["vid"] not in state.service.hh_applied
                    ]
                resumes = [
                    {"id": r["id"], "title": r.get("title") or r["id"]}
                    for r in state.service.resumes
                ]
                self._json(
                    200,
                    {
                        "items": items,
                        "dry_run": state.service.dry_run,
                        "resumes": resumes,
                        "age": state.service.age_days(),
                    },
                )
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if not self._api_ok():
                self._json(403, {"error": "forbidden"})
                return
            state.last_ping = time.monotonic()
            path = urlsplit(self.path).path
            data = self._body()
            vid = str(data.get("vid") or "")
            service = state.service
            if path == "/api/ping":
                self._json(200, {"ok": True})
                return
            if not vid:
                self._json(400, {"error": "нет vid"})
                return
            if path == "/api/send":
                letter = data.get("letter")
                outcome = service.send(
                    vid,
                    str(letter) if isinstance(letter, str) else None,
                    confirm=bool(data.get("confirm")),
                    resume_id=str(data.get("resume_id") or "") or None,
                )
            elif path == "/api/skip":
                outcome = service.skip(vid)
            elif path == "/api/rewrite":
                outcome = service.rewrite(vid)
            elif path == "/api/requeue":
                outcome = service.requeue(vid)
            else:
                self._send(404, b"not found", "text/plain")
                return
            self._json(200, outcome.to_json())

    return Handler


def make_server(
    service: ApproveService, port: int = 0, token: str | None = None
) -> tuple[ThreadingHTTPServer, WebState, str]:
    state = WebState(service, token or secrets.token_urlsafe(18))
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state))
    real_port = server.server_address[1]
    state.allowed_hosts = {f"127.0.0.1:{real_port}", f"localhost:{real_port}"}
    url = f"http://127.0.0.1:{real_port}/?t={state.token}"
    return server, state, url


def _watchdog(server: ThreadingHTTPServer, state: WebState) -> None:
    while True:
        time.sleep(5)
        if time.monotonic() - state.last_ping > IDLE_TIMEOUT:
            print("Страница закрыта — сервер отбора завершается.")
            server.shutdown()
            return


ARCHIVE_SWEEP_EVERY = 15 * 60


def _archive_sweeper(state: WebState, stop: threading.Event) -> None:
    """Пока страница открыта — убирает из очереди вакансии, ушедшие в архив.
    Страница сама перечитывает очередь, убранные исчезают при обновлении."""
    while not stop.is_set():
        try:
            # медленно: карточки hh отдаёт ограниченно, они нужнее поиску
            state.service.sweep_archived(stop=stop, pause=15.0)
        except Exception as ex:  # noqa: BLE001 — сверка не должна ронять сервер
            print(f"[архив] сверка не удалась: {ex}")
        stop.wait(ARCHIVE_SWEEP_EVERY)


def run_web(args: argparse.Namespace) -> int:
    from .cli import _load_toml
    from .hh_tool import SavingHHApplicantTool
    from .sne import (
        APPROVE_LOCK_NAME,
        APPROVE_LOCK_STALE,
        _acquire_run_lock,
        _release_run_lock,
    )

    data_dir = Path(args.seen_store).parent
    if sys.stdout is None:  # pythonw из джобы: консоли нет — пишем в лог
        data_dir.mkdir(parents=True, exist_ok=True)
        log = open(data_dir / "web.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stderr = log
        print(f"===== {time.strftime('%d.%m.%Y %H:%M:%S')} =====")
    url_file = data_dir / WEB_URL_FILE
    lock_path = data_dir / APPROVE_LOCK_NAME
    lock = _acquire_run_lock(lock_path, stale_after=APPROVE_LOCK_STALE)
    if lock is None:
        if url_file.exists():
            url = url_file.read_text(encoding="utf-8").strip()
            print(f"Страница отбора уже открыта: {url}")
            if not args.no_browser:
                webbrowser.open(url)
        else:
            print("Сейчас открыто консольное окно approve — закончите в нём.")
        return 0
    try:
        tool = SavingHHApplicantTool()
        try:
            service = ApproveService(
                args,
                _load_toml(args.config),
                tool.api_client,
                dry_run=args.dry_run,
                session=getattr(tool, "session", None),
            )
        except RuntimeError as ex:
            print(f"Ошибка: {ex}", file=sys.stderr)
            return 2
        server, state, url = make_server(service, port=args.port)
        url_file.parent.mkdir(parents=True, exist_ok=True)
        url_file.write_text(url, encoding="utf-8")
        print(f"Страница отбора: {url}")
        print("Закройте вкладку — сервер сам завершится через ~3 минуты.")
        if not args.no_browser:
            webbrowser.open(url)
        threading.Thread(target=_watchdog, args=(server, state), daemon=True).start()
        stop_sweep = threading.Event()
        threading.Thread(
            target=_archive_sweeper, args=(state, stop_sweep), daemon=True
        ).start()
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            stop_sweep.set()
            server.server_close()
            if not args.dry_run:
                tool._persist()
        return 0
    finally:
        try:
            url_file.unlink()
        except OSError:
            pass
        _release_run_lock(lock_path, lock)


PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Отбор вакансий</title>
<style>
:root{--bg:#f6f6f8;--card:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e3e3e8;
--accent:#0a66c2;--ok:#1f8a4c;--mid:#b26a00;--bad:#b3261e;--warnbg:#fff4e0;}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--card:#1c1c1f;--fg:#f2f2f7;
--muted:#a1a1a6;--line:#2c2c30;--accent:#4a9eff;--ok:#4cc27c;--mid:#f0a33a;--bad:#ff6b60;--warnbg:#3a2a10;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px}
header h1{margin:0;font-size:18px}
.sub{color:var(--muted);font-size:13px}
.dry{background:var(--warnbg);color:var(--mid);padding:6px 10px;border-radius:6px;margin-top:6px;font-size:13px;display:none}
main{max-width:860px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.top{display:flex;gap:12px;align-items:flex-start}
.score{min-width:52px;text-align:center;font-weight:700;font-size:22px;border-radius:10px;padding:4px 6px;border:2px solid currentColor}
.hi{color:var(--ok)}.md{color:var(--mid)}.lo{color:var(--bad)}
.title{font-weight:650;font-size:17px;color:var(--fg);text-decoration:none}
.title:hover{text-decoration:underline}
.meta{color:var(--muted);font-size:14px}
ul{margin:6px 0 0;padding-left:20px}
.ok{color:var(--ok)}.miss{color:var(--mid)}
textarea{width:100%;min-height:190px;margin-top:10px;padding:10px;border:1px solid var(--line);border-radius:8px;
background:var(--bg);color:var(--fg);font:inherit;resize:vertical}
.wc{color:var(--muted);font-size:12px;text-align:right}
.warn{background:var(--warnbg);color:var(--mid);border-radius:8px;padding:8px 10px;margin-top:8px;font-size:14px}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
button{font:inherit;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);padding:8px 14px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button:disabled{opacity:.5;cursor:wait}
.status{margin-top:8px;font-size:14px}
.empty{text-align:center;color:var(--muted);padding:48px 0}
.tabs{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap}
.tab{border-radius:999px;padding:6px 14px}
.tab.on{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px;color:var(--muted);font-size:14px}
.tools input{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);min-width:0;flex:1 1 200px}
.hidden{display:none}
.qtools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px;color:var(--muted);font-size:14px}
.req{background:var(--warnbg);border:1px solid var(--mid);color:var(--fg);border-radius:8px;padding:8px 10px;margin-top:10px;font-size:14px}
.req b{color:var(--mid)}
.resp{font-weight:600}
details{margin-top:6px}
.resume{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:10px;font-size:14px;color:var(--muted)}
span.src{display:inline-block;margin-left:8px;padding:1px 8px;border-radius:999px;font-size:12px;background:#eef6ee;color:#2e7d32;border:1px solid #cfe6cf}
input.age{font:inherit;width:4.5em;padding:5px 6px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
select{font:inherit;padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);max-width:100%}
</style></head><body>
<header><h1>Отбор вакансий <span id="count" class="sub"></span></h1>
<div class="sub">Откликнуться — отправить с письмом ниже · Пропустить — больше не показывать · Позже — скрыть до обновления страницы</div>
<div class="dry" id="dry">Пробный режим: отклики НЕ отправляются и ничего не записывается.</div>
<div class="tabs"><button class="tab on" id="tab1">На рассмотрение</button><button class="tab" id="tab2">Не прошли порог</button></div></header>
<main id="list"><div class="qtools"><span>Сортировка:</span>
<select id="qsrc"><option value="all">Все источники</option><option value="hh">hh</option><option value="geekjob">GeekJob</option><option value="habr">Habr Career</option><option value="telegram">Telegram</option><option value="superjob">SuperJob</option></select>
<select id="qsort"><option value="score">По скору</option><option value="date">По дате выхода вакансии</option><option value="both">Скор и свежесть вместе</option></select>
<span id="qhint"></span>
<span>Вышла от <input id="agemin" class="age" type="number" min="0" step="1"> до <input id="agemax" class="age" type="number" min="0" step="1"> дн. назад</span><span id="agehint"></span></div>
<div id="qlist"><div class="empty">Загружаю…</div></div></main>
<main id="rejected" class="hidden"><div class="tools">
<span id="rjinfo"></span>
<input id="rjq" type="search" placeholder="Поиск по названию, компании, причине">
<select id="rjsrc"><option value="all">Все источники</option><option value="hh">hh</option><option value="geekjob">GeekJob</option><option value="habr">Habr Career</option><option value="telegram">Telegram</option><option value="superjob">SuperJob</option></select>
<select id="rjsort"><option value="date">По дате выхода вакансии</option><option value="score">По скору</option><option value="both">Скор и свежесть вместе</option></select>
</div><div id="rjlist"><div class="empty">Загружаю…</div></div></main>
<script>
const SRC={geekjob:"GeekJob",habr:"Habr Career",telegram:"Telegram",superjob:"SuperJob"};

const TOKEN="__TOKEN__", DRY=__DRY__;
const hidden=new Set(); const drafts={}; const chosenResume={}; let busy=false; let RESUMES=[];
if(DRY) document.getElementById("dry").style.display="block";
async function api(path, body){
  const opt={headers:{"X-Token":TOKEN,"Content-Type":"application/json"}};
  if(body!==undefined){opt.method="POST";opt.body=JSON.stringify(body);}
  const r=await fetch(path,opt); if(!r.ok) throw new Error("HTTP "+r.status); return r.json();
}
function el(tag, cls, text){const e=document.createElement(tag); if(cls) e.className=cls; if(text!=null) e.textContent=text; return e;}
function words(t){return (t.trim().match(/\S+/g)||[]).length;}
function list(title, arr, cls){ if(!arr||!arr.length) return null;
  const d=el("div",cls); d.appendChild(el("b",null,title+": ")); d.appendChild(document.createTextNode(arr.join("; "))); return d; }
function warnBox(v){ const w=el("div","warn"); w.appendChild(el("b",null,"Проверка письма: "));
  w.appendChild(document.createTextNode(v.join("; "))); return w; }
// Сортировки. «Скор и свежесть вместе»: скор минус 2 балла за каждый
// день с публикации — свежая вакансия на 70 обгоняет недельную на 80.
const SORT_HINT={score:"сначала выше скор", date:"сначала новые вакансии",
  both:"скор минус 2 балла за каждый день с публикации"};
function sortRows(rows, mode){
  const now=Date.now()/1000, sc=r=>typeof r.score==="number"?r.score:-1, pub=r=>r.pub_ts||r.ts||0;
  const blend=r=>sc(r)-2*Math.max(0,(now-pub(r))/86400);
  const cmp={score:(a,b)=>(sc(b)-sc(a))||(pub(b)-pub(a)), date:(a,b)=>(pub(b)-pub(a))||(sc(b)-sc(a)),
    both:(a,b)=>(blend(b)-blend(a))||(sc(b)-sc(a))}[mode]||((a,b)=>sc(b)-sc(a));
  return rows.slice().sort(cmp);
}
function savedSort(key, def){ try{ return localStorage.getItem(key)||def; }catch(e){ return def; } }
function saveSort(key, v){ try{ localStorage.setItem(key, v); }catch(e){} }
let QITEMS=[];
const qsort=document.getElementById("qsort"); qsort.value=savedSort("snpr.qsort","score");
document.getElementById("qhint").textContent=SORT_HINT[qsort.value]||"";
qsort.addEventListener("change",()=>{ saveSort("snpr.qsort", qsort.value);
  document.getElementById("qhint").textContent=SORT_HINT[qsort.value]||""; render(QITEMS); });
// Возраст с публикации, дней. Пустое поле — без границы. Начальные
// значения — [search] min_age_days / max_age_days из config.toml.
let AGE_INIT=false;
function ageFilter(){
  const v=id=>{ const s=document.getElementById(id).value.trim(); return s===""?null:Number(s); };
  const lo=v("agemin"), hi=v("agemax"), now=Date.now()/1000;
  return i=>{ if(!i.pub_ts) return true; const d=(now-i.pub_ts)/86400;
    return (lo===null||d>=lo) && (hi===null||hi<=0||d<=hi); };
}
for(const id of ["agemin","agemax"]) document.getElementById(id).addEventListener("input",()=>render(QITEMS));
// Источник вакансии: hh / GeekJob (выбор запоминается в браузере)
function srcFilter(id){ const v=document.getElementById(id).value; return i=>v==="all"||(i.source||"hh")===v; }
for(const id of ["qsrc","rjsrc"]){ const s=document.getElementById(id); s.value=savedSort("snpr."+id,"all");
  s.addEventListener("change",()=>{ saveSort("snpr."+id, s.value); id==="qsrc"?render(QITEMS):renderRejected(); }); }
function render(items){
  QITEMS=items;
  const box=document.getElementById("qlist"); box.textContent="";
  const inAge=ageFilter(), inSrc=srcFilter("qsrc"); const visible=items.filter(i=>!hidden.has(i.vid));
  const shown=sortRows(visible.filter(i=>inAge(i)&&inSrc(i)), qsort.value);
  document.getElementById("agehint").textContent = shown.length<visible.length ? "· скрыто фильтрами: "+(visible.length-shown.length) : "";
  document.getElementById("count").textContent = shown.length ? "· ждут решения: "+shown.length : "";
  document.title = shown.length ? "("+shown.length+") Отбор вакансий" : "Отбор вакансий";
  if(!shown.length){ box.appendChild(el("div","empty", hidden.size ? "Остальные отложены — обновите страницу, чтобы увидеть их снова." : "Очередь пуста. Новые вакансии появятся здесь сами.")); return; }
  for(const it of shown) box.appendChild(card(it));
}
function pubText(it){
  if(it.age_hours==null) return "";
  if(it.age_hours<1) return "опубликована меньше часа назад";
  if(it.age_hours<48) return "опубликована "+Math.round(it.age_hours)+" ч назад";
  const d=new Date((it.pub_ts||0)*1000);
  return "опубликована "+String(d.getDate()).padStart(2,"0")+"."+String(d.getMonth()+1).padStart(2,"0")
    +" ("+Math.round(it.age_hours/24)+" дн. назад)";
}
function card(it){
  const c=el("section","card");
  const top=el("div","top");
  const s=el("div","score "+(it.score>=75?"hi":it.score>=55?"md":"lo"), it.score==null?"—":String(it.score));
  const head=el("div"); const a=el("a","title",it.name); a.href=it.url; a.target="_blank"; a.rel="noopener noreferrer";
  head.appendChild(a);
  const GJ = it.source!=="hh" && !!SRC[it.source], SRCN=SRC[it.source]||"";
  if(GJ) head.appendChild(el("span","src",SRCN));
  head.appendChild(el("div","meta",[it.employer,it.area,it.work_format].filter(Boolean).join(" · ")));
  const extra=[it.salary||"зарплата не указана", pubText(it)].filter(Boolean).join(" · ");
  head.appendChild(el("div","meta",extra));
  if(!GJ){ const respLine=el("div","meta resp","откликов: …"); head.appendChild(respLine);
    loadResponses(it.vid, respLine); }
  top.appendChild(s); top.appendChild(head); c.appendChild(top);
  if(it.flags&&it.flags.length) c.appendChild(el("div","req","⚠️ проверьте работодателя: "+it.flags.join(", ")));
  if(it.reasons&&it.reasons.length){ const ul=el("ul"); it.reasons.slice(0,4).forEach(r=>ul.appendChild(el("li",null,r))); c.appendChild(ul); }
  const m=list("Совпало",it.matched,"ok"); if(m) c.appendChild(m);
  const x=list("Не хватает",it.missing,"miss"); if(x) c.appendChild(x);
  const ta=el("textarea"); ta.value = drafts[it.vid]!=null ? drafts[it.vid] : it.letter;
  const wc=el("div","wc"); const upd=()=>{wc.textContent=words(ta.value)+" слов"; drafts[it.vid]=ta.value;}; ta.addEventListener("input",upd); upd();
  c.appendChild(ta); c.appendChild(wc);
  const warnSlot=el("div"); if(it.violations&&it.violations.length) warnSlot.appendChild(warnBox(it.violations)); c.appendChild(warnSlot);
  if(it.requests&&it.requests.length){ const rq=el("div","req");
    rq.appendChild(el("b",null,"Просьбы работодателя в вакансии — проверьте, что письмо их выполняет:"));
    const ul=el("ul"); it.requests.forEach(x=>ul.appendChild(el("li",null,x))); rq.appendChild(ul); c.appendChild(rq); }
  const rs=el("label","resume"); rs.appendChild(el("span",null,"Резюме для отклика: "));
  const sel=el("select"); RESUMES.forEach(r=>{const o=el("option",null,r.title); o.value=r.id; sel.appendChild(o);});
  sel.value = chosenResume[it.vid] || it.resume_id; sel.disabled = RESUMES.length<2;
  sel.addEventListener("change",()=>{chosenResume[it.vid]=sel.value;});
  rs.appendChild(sel); if(!GJ) c.appendChild(rs);
  const st=el("div","status");
  const btns=el("div","btns");
  const bSend=el("button","primary",GJ?"Я откликнулся":"Откликнуться"), bSkip=el("button",null,"Пропустить"),
        bLater=el("button",null,"Позже"), bRe=el("button",null,"Переписать письмо");
  // GeekJob/Habr/Telegram: бот откликнуться не может — открываем вакансию и копируем письмо
  const TG = it.source==="telegram";
  const bOpen=el("button","primary",TG?"Открыть пост и скопировать письмо":"Открыть на "+SRCN+" и скопировать письмо");
  const where = TG ? "отправьте его по контакту из поста" : "вставьте его в отклик на "+SRCN;
  bOpen.onclick=async()=>{ window.open(it.url,"_blank","noopener");
    try{ await navigator.clipboard.writeText(ta.value); st.textContent="Письмо скопировано — "+where+", потом нажмите «Я откликнулся»."; }
    catch(e){ ta.select(); st.textContent="Скопируйте письмо (Ctrl+C), "+where+", потом нажмите «Я откликнулся»."; }
    st.className="status"; };
  const all=[bSend,bSkip,bLater,bRe,bOpen]; const lock=v=>{all.forEach(b=>b.disabled=v); busy=v;};
  async function act(fn){ lock(true); try{ await fn(); } catch(e){ st.textContent="Ошибка: "+e.message; st.className="status lo"; } finally{ lock(false); } }
  function finish(res){ st.textContent=res.message; st.className="status "+(res.closed||res.status==="dry_run"?"ok":"lo");
    if(res.closed){ delete drafts[it.vid]; setTimeout(()=>{c.remove(); refresh();},900); } }
  bSend.onclick=()=>act(async()=>{
    let res=await api("/api/send",{vid:it.vid,letter:ta.value,resume_id:sel.value});
    if(res.status==="needs_confirm"){ warnSlot.textContent=""; warnSlot.appendChild(warnBox(res.violations));
      if(!confirm(res.message+"\n\n"+res.violations.join("\n"))) { st.textContent="Не отправлено."; return; }
      res=await api("/api/send",{vid:it.vid,letter:ta.value,confirm:true,resume_id:sel.value}); }
    finish(res);
  });
  bSkip.onclick=()=>act(async()=>finish(await api("/api/skip",{vid:it.vid})));
  bLater.onclick=()=>{ hidden.add(it.vid); c.remove(); refresh(); };
  bRe.onclick=()=>act(async()=>{ st.textContent="Пишу новое письмо…"; st.className="status";
    const res=await api("/api/rewrite",{vid:it.vid});
    if(res.status==="rewritten"){ ta.value=res.letter; upd(); warnSlot.textContent="";
      if(res.violations.length) warnSlot.appendChild(warnBox(res.violations)); st.textContent=res.message; st.className="status ok"; }
    else finish(res); });
  if(GJ) btns.append(bOpen,bSend,bSkip,bLater,bRe); else btns.append(bSend,bSkip,bLater,bRe);
  c.appendChild(btns); c.appendChild(st);
  return c;
}
let last="";
async function refresh(){
  if(busy) return;
  try{ const d=await api("/api/queue"); RESUMES=d.resumes||[];
    if(!AGE_INIT && d.age){ AGE_INIT=true;
      document.getElementById("agemin").value = d.age[1]>0 ? d.age[0] : "";
      document.getElementById("agemax").value = d.age[1]>0 ? d.age[1] : ""; }
 const key=JSON.stringify(d.items.map(i=>[i.vid,i.letter,i.resume_id]));
    if(key!==last || !document.querySelector(".card")){ last=key; render(d.items); } }
  catch(e){ document.getElementById("qlist").textContent="Сервер отбора не отвечает — запустите python -m snpr web заново."; }
}
const RESP={}; let respChain=Promise.resolve();
function respText(n, blocked){ return n!=null ? "откликов: "+n
  : blocked ? "откликов: нет данных (сайт hh.ru просит проверку VPN)" : "откликов: нет данных"; }
function loadResponses(vid, node){
  if(vid in RESP){ node.textContent=respText(RESP[vid]); return; }
  // по одной странице hh за раз — не нагружаем сайт
  respChain=respChain.then(async()=>{ try{ const d=await api("/api/responses?vid="+encodeURIComponent(vid));
    if(d.count!=null) RESP[vid]=d.count; node.textContent=respText(d.count, d.blocked); }catch(e){ node.textContent=respText(null); } });
}
let RJ=[], RJTH=null;
function showTab(n){
  document.getElementById("list").classList.toggle("hidden", n!==1);
  document.getElementById("rejected").classList.toggle("hidden", n!==2);
  document.getElementById("tab1").classList.toggle("on", n===1);
  document.getElementById("tab2").classList.toggle("on", n===2);
  if(n===2) loadRejected();
}
document.getElementById("tab1").onclick=()=>showTab(1);
document.getElementById("tab2").onclick=()=>showTab(2);
document.getElementById("rjq").addEventListener("input", renderRejected);
const rjsort=document.getElementById("rjsort"); rjsort.value=savedSort("snpr.rjsort","date");
rjsort.addEventListener("change", ()=>{ saveSort("snpr.rjsort", rjsort.value); renderRejected(); });
function setRjCount(){ document.getElementById("tab2").textContent="Не прошли порог ("+RJ.length+")"; }
async function loadRejected(){
  try{ const d=await api("/api/rejected"); RJ=d.items; RJTH=d.threshold; setRjCount(); renderRejected(); }
  catch(e){ document.getElementById("rjlist").textContent="Не удалось загрузить: "+e.message; }
}
function renderRejected(){
  const q=document.getElementById("rjq").value.trim().toLowerCase();
  const sort=document.getElementById("rjsort").value;
  const inSrc=srcFilter("rjsrc");
  let rows=RJ.filter(r=>inSrc(r) && (!q || [r.name,r.employer,r.area,(r.reasons||[]).join(" ")].join(" ").toLowerCase().includes(q)));
  rows=sortRows(rows, sort);
  document.getElementById("rjinfo").textContent="Скор ниже "+RJTH+" (maybe_min в config.toml) · показано "+rows.length+" из "+RJ.length;
  const box=document.getElementById("rjlist"); box.textContent="";
  if(!rows.length){ box.appendChild(el("div","empty","Пусто.")); return; }
  for(const r of rows) box.appendChild(rejCard(r));
}
function rejCard(r){
  const c=el("section","card"); const top=el("div","top");
  top.appendChild(el("div","score "+(r.score>=40?"md":"lo"), String(r.score)));
  const head=el("div"); const a=el("a","title",r.name); a.href=r.url; a.target="_blank"; a.rel="noopener noreferrer"; head.appendChild(a);
  if(SRC[r.source]) head.appendChild(el("span","src",SRC[r.source]));
  head.appendChild(el("div","meta",[r.employer,r.area,r.work_format].filter(Boolean).join(" · ")));
  head.appendChild(el("div","meta",[r.salary||"зарплата не указана", r.pub_ts?("вышла "+new Date(r.pub_ts*1000).toLocaleDateString("ru-RU")):"", r.date?("оценена "+r.date):""].filter(Boolean).join(" · ")));
  top.appendChild(head); c.appendChild(top);
  if(r.reasons.length){ const ul=el("ul"); r.reasons.forEach(x=>ul.appendChild(el("li",null,x))); c.appendChild(ul); }
  if(r.matched.length||r.missing.length){ const d=el("details"); d.appendChild(el("summary",null,"Совпало / не хватает"));
    const m=list("Совпало",r.matched,"ok"); if(m) d.appendChild(m);
    const x=list("Не хватает",r.missing,"miss"); if(x) d.appendChild(x); c.appendChild(d); }
  const st=el("div","status"); const btns=el("div","btns");
  const b=el("button",null,"Вернуть на рассмотрение");
  b.onclick=async()=>{ b.disabled=true; st.textContent="Пишу письмо…"; st.className="status";
    try{ const res=await api("/api/requeue",{vid:r.vid}); st.textContent=res.message;
      st.className="status "+(res.status==="requeued"||res.status==="dry_run"?"ok":"lo");
      if(res.status==="requeued"){ RJ=RJ.filter(x=>x.vid!==r.vid); setTimeout(()=>{ c.remove(); setRjCount(); last=""; refresh(); },900); }
      else b.disabled=false; }
    catch(e){ st.textContent="Ошибка: "+e.message; st.className="status lo"; b.disabled=false; } };
  btns.appendChild(b); c.appendChild(btns); c.appendChild(st); return c;
}
refresh(); loadRejected(); setInterval(refresh,60000); setInterval(()=>api("/api/ping",{}).catch(()=>{}),30000);
</script></body></html>
"""
