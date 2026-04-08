"""
Serveur Flask pour l'interface de contrôle de l'automatisation LBA.
"""

import asyncio
import json
import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

app = Flask(__name__)
app.config["JSON_ENSURE_ASCII"] = False

# ---------------------------------------------------------------------------
# État global
# ---------------------------------------------------------------------------

log_queue: queue.Queue = queue.Queue(maxsize=500)
stop_event: threading.Event = threading.Event()
current_status: dict = {
    "status": "stopped",
    "sent_today": 0,
    "skipped": 0,
    "errors": 0,
    "current_city": "",
    "current_job": "",
    "current_company": "",
}
_scheduler_thread: threading.Thread | None = None

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

CONFIG_FILE = Path("config.json")
DEFAULT_CONFIG = {
    "lastname": "",
    "firstname": "",
    "email": "",
    "phone": "",
    "cv_path": "",
    "message_template": (
        "Bonjour,\n\n"
        "Je suis actuellement à la recherche d'une alternance dans le domaine du développement web.\n\n"
        "Après avoir découvert {company} sur La Bonne Alternance, votre entreprise m'a particulièrement intéressé "
        "et je souhaite vous adresser ma candidature spontanée.\n\n"
        "Je suis motivé, curieux(se) et disponible pour un entretien à votre convenance.\n\n"
        "Cordialement,\n{firstname} {lastname}"
    ),
    "selected_cities": ["Strasbourg", "Nantes", "Lyon", "Metz", "Nancy", "Marseille"],
    "job_searches": [
        {
            "name": "Développement web, intégration",
            "romes": "M1805,M1855,M1825,M1834,M1861,E1210,E1405,M1865,M1877,M1886,M1887",
        }
    ],
    "headless": False,
    "delay_between_applications": 3,
}


def get_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Callbacks used by automation
# ---------------------------------------------------------------------------

def add_log(entry: dict) -> None:
    try:
        log_queue.put_nowait(entry)
    except queue.Full:
        try:
            log_queue.get_nowait()
            log_queue.put_nowait(entry)
        except Exception:
            pass


def update_status(stats: dict) -> None:
    current_status.update(stats)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/config")
def api_get_config():
    return jsonify(get_config())


@app.post("/api/config")
def api_save_config():
    save_config(request.get_json(force=True))
    return jsonify({"ok": True})


@app.post("/api/start")
def api_start():
    global stop_event

    if current_status.get("status") == "running":
        return jsonify({"error": "Automation déjà en cours"}), 400

    cfg = request.get_json(force=True) or get_config()
    save_config(cfg)

    # Flush logs
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break

    stop_event = threading.Event()

    def _run():
        from automation import LBAAutomation

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        auto = LBAAutomation(
            config=cfg,
            stop_event=stop_event,
            callbacks={"log": add_log, "status": update_status},
        )
        try:
            loop.run_until_complete(auto.run())
        except Exception as exc:
            add_log({
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": f"Erreur critique: {exc}",
                "level": "error",
            })
            update_status({"status": "stopped"})
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    update_status({"status": "running", "sent_today": 0, "skipped": 0, "errors": 0})
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    stop_event.set()
    update_status({"status": "stopping"})
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    return jsonify(current_status)


@app.get("/api/logs/stream")
def api_logs_stream():
    """Server-Sent Events pour les logs en temps réel."""

    def _generate():
        while True:
            try:
                entry = log_queue.get(timeout=2.0)
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield ": heartbeat\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/sent")
def api_sent():
    p = Path("sent_applications.json")
    if p.exists():
        try:
            return jsonify(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return jsonify([])


@app.post("/api/schedule_midnight")
def api_schedule_midnight():
    """Programme un redémarrage automatique à 00:00 demain."""
    global _scheduler_thread

    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=5, microsecond=0
    )
    delay = (tomorrow - now).total_seconds()

    def _wait_and_start():
        add_log({
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": f"⏰ Reprise programmée à {tomorrow.strftime('%Y-%m-%d 00:00:05')}",
            "level": "info",
        })
        time.sleep(delay)
        add_log({
            "time": "00:00",
            "message": "⏰ Redémarrage automatique à minuit",
            "level": "info",
        })
        # Déclencher le démarrage via une requête interne
        with app.test_request_context():
            cfg = get_config()
        stop_event_local = threading.Event()

        def _inner_run():
            from automation import LBAAutomation

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            auto = LBAAutomation(
                config=cfg,
                stop_event=stop_event_local,
                callbacks={"log": add_log, "status": update_status},
            )
            try:
                loop.run_until_complete(auto.run())
            except Exception as exc:
                add_log({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": f"Erreur critique (minuit): {exc}",
                    "level": "error",
                })
                update_status({"status": "stopped"})
            finally:
                loop.close()

        t = threading.Thread(target=_inner_run, daemon=True)
        t.start()
        update_status({"status": "running", "sent_today": 0, "skipped": 0, "errors": 0})

    _scheduler_thread = threading.Thread(target=_wait_and_start, daemon=True)
    _scheduler_thread.start()

    return jsonify({"scheduled": tomorrow.isoformat()})


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False, port=5000, threaded=True)
