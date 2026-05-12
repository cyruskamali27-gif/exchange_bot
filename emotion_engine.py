"""
emotion_engine.py — Dual-layer emotion analysis.

Layer 1: Rule-based keywords (synchronous, instant — used for current reply).
Layer 2: Hume AI Expression Measurement (async background — enriches DB).

Falls back gracefully if Hume is unavailable.
"""

import asyncio
import json
import logging
import sqlite3
import time as _time
from collections import Counter
from datetime import datetime

from config import HUME_API_KEY, ENABLE_HUME_EMOTION

log = logging.getLogger("emotion_engine")
# All Hume-related entries are prefixed [HUME]; rule-based entries use [EMOTION]
DB_PATH = "/var/www/exchange_bot/exchange.db"

# ─── Keyword sets ─────────────────────────────────────────────────────────────

_URGENT     = ["زود باش", "سریع", "وقت ندارم", "عجله دارم", "اورژانسی", "فوری", "الان باید", "خیلی فوریه"]
_SUSPICIOUS = ["مطمئنی", "مطمئن هستی", "کلاهبرد", "چرا باید", "اثبات کن", "مدرک بده",
               "از کجا بدونم", "قابل اعتماد", "ضمانت", "چطور مطمئن"]
_NEGOTIATOR = ["گرونه", "ارزون‌تر", "تخفیف", "بهتر نمیشه", "جای دیگه", "می‌دن",
               "کمتر نمیشه", "راهی نیست", "آخرین قیمت", "بیشتر نمیشه", "فرق داره"]
_IMPATIENT  = ["سریع", "زود", "الان", "عجله", "دیر", "وقت ندارم", "طول میکشه", "تند باش"]
_FRIENDLY   = ["ممنون", "مرسی", "خوب", "عالی", "باشه", "ممنونم", "دست گلت",
               "لطف کردید", "خیلی ممنون", "مهربونی", "ممنون از شما"]


# ─── Rule-based analysis ──────────────────────────────────────────────────────

def analyze_text(text: str) -> dict:
    """Fast synchronous emotion analysis from keyword matching."""
    t = text.lower() if text else ""
    words = t.split()

    urgency_score       = min(sum(1 for w in _URGENT if w in t) * 0.45, 1.0)
    suspicion_score     = min(sum(1 for w in _SUSPICIOUS if w in t) * 0.4, 1.0)
    negotiation_pressure = min(sum(1 for w in _NEGOTIATOR if w in t) * 0.45, 1.0)
    friendly_score      = min(sum(1 for w in _FRIENDLY if w in t) * 0.35, 1.0)
    impatient_score     = min(sum(1 for w in _IMPATIENT if w in t) * 0.35, 1.0)

    # Short messages → impatient signal
    if len(words) <= 3:
        impatient_score = min(impatient_score + 0.3, 1.0)

    # Multiple question marks → suspicious/doubtful
    q_marks = t.count("؟") + t.count("?")
    if q_marks >= 2:
        suspicion_score = min(suspicion_score + 0.25, 1.0)

    # Exclamations → urgency/anger
    if text.count("!") >= 2:
        urgency_score = min(urgency_score + 0.2, 1.0)

    scores = {
        "urgent":     urgency_score,
        "suspicious": suspicion_score,
        "negotiator": negotiation_pressure,
        "friendly":   friendly_score,
        "impatient":  impatient_score,
    }

    max_score = max(scores.values())
    if max_score < 0.15:
        dominant   = "serious" if len(words) <= 4 else "neutral"
        confidence = 0.3
    else:
        dominant   = max(scores, key=scores.get)
        confidence = min(max_score * 1.5, 1.0)

    trust_score       = min(friendly_score + (0.1 if len(words) > 10 else 0), 1.0)
    emotional_energy  = min(urgency_score + impatient_score, 1.0)
    emotional_stability = max(0.0, 1.0 - (urgency_score + suspicion_score))

    return {
        "dominant_emotion":       dominant,
        "emotion_confidence":     round(confidence, 2),
        "urgency_score":          round(urgency_score, 2),
        "trust_score":            round(trust_score, 2),
        "suspicion_score":        round(suspicion_score, 2),
        "negotiation_pressure":   round(negotiation_pressure, 2),
        "emotional_energy":       round(emotional_energy, 2),
        "emotional_stability":    round(emotional_stability, 2),
        "source":                 "rule_based",
    }


# ─── DB helpers ───────────────────────────────────────────────────────────────

def save_emotion(uid: int, emotion_data: dict, conv_id: int = None):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT INTO emotion_history
               (user_id, emotion, dominant_emotion, urgency_score, trust_score,
                suspicion_score, negotiation_pressure, emotional_energy,
                emotional_stability, emotion_confidence, source, detected_at, conversation_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uid),
                emotion_data["dominant_emotion"],
                emotion_data["dominant_emotion"],
                emotion_data["urgency_score"],
                emotion_data["trust_score"],
                emotion_data["suspicion_score"],
                emotion_data["negotiation_pressure"],
                emotion_data["emotional_energy"],
                emotion_data["emotional_stability"],
                emotion_data["emotion_confidence"],
                emotion_data.get("source", "rule_based"),
                datetime.now().isoformat(),
                conv_id,
            )
        )
        conn.execute(
            "UPDATE customer_profiles SET mood=?, last_seen=? WHERE user_id=?",
            (emotion_data["dominant_emotion"], datetime.now().isoformat(), str(uid))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("[EMOTION] save_emotion: %s", e)


def get_emotion_history(uid: int, limit: int = 10) -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """SELECT dominant_emotion, urgency_score, trust_score, suspicion_score,
                      negotiation_pressure, detected_at
               FROM emotion_history WHERE user_id=?
               ORDER BY id DESC LIMIT ?""",
            (str(uid), limit)
        ).fetchall()
        conn.close()
        return [
            {"emotion": r[0], "urgency": r[1], "trust": r[2],
             "suspicion": r[3], "negotiation": r[4], "at": r[5]}
            for r in rows
        ]
    except Exception:
        return []


def get_emotional_fingerprint(uid: int) -> dict:
    """Aggregate long-term emotional pattern for this user."""
    history = get_emotion_history(uid, limit=20)
    if not history:
        return {"dominant_pattern": "unknown", "avg_trust": 0.5,
                "avg_suspicion": 0.1, "avg_urgency": 0.1, "emotion_counts": {}}
    counts   = Counter(h["emotion"] for h in history)
    dominant = counts.most_common(1)[0][0]
    return {
        "dominant_pattern": dominant,
        "avg_trust":       round(sum(h["trust"] for h in history) / len(history), 2),
        "avg_suspicion":   round(sum(h["suspicion"] for h in history) / len(history), 2),
        "avg_urgency":     round(sum(h["urgency"] for h in history) / len(history), 2),
        "emotion_counts":  dict(counts),
    }


# ─── Hume AI enrichment (background) ─────────────────────────────────────────

_hume_rate_limit_until: float = 0.0
_HUME_COOLDOWN_S = 300  # 5-minute cooldown after any rate-limit or auth error


async def _hume_enrich(uid: int, text: str, base: dict, conv_id: int = None):
    """
    Run Hume AI batch inference in background. Updates DB on completion.
    Skips silently during cooldown. Never crashes the calling coroutine.
    """
    global _hume_rate_limit_until

    if not ENABLE_HUME_EMOTION or not HUME_API_KEY:
        return
    if _time.time() < _hume_rate_limit_until:
        log.debug("[HUME] in cooldown (%.0f s left), skipping uid=%s",
                  _hume_rate_limit_until - _time.time(), uid)
        return

    def _call():
        try:
            from hume import HumeClient
            from hume.expression_measurement.batch import Models, Language

            client  = HumeClient(api_key=HUME_API_KEY)
            job_result = client.expression_measurement.batch.start_inference_job(
                models=Models(language=Language()),
                text=[text],
            )
            # SDK may return a string job_id or an object with .job_id
            job_id = job_result if isinstance(job_result, str) else job_result.job_id
            # Poll max 12 s
            for _ in range(24):
                _time.sleep(0.5)
                details = client.expression_measurement.batch.get_job_details(id=job_id)
                status_raw = details.state.status
                status = status_raw.value if hasattr(status_raw, "value") else str(status_raw)
                if status == "COMPLETED":
                    return client.expression_measurement.batch.get_job_predictions(id=job_id)
                if status == "FAILED":
                    return None
            return None
        except Exception as e:
            log.debug("[HUME] _call error: %s", e)
            raise   # re-raise so the outer handler can inspect

    try:
        predictions = await asyncio.wait_for(asyncio.to_thread(_call), timeout=20)
        if not predictions:
            return

        enriched = base.copy()
        enriched["source"] = "hume_ai"

        for pred in predictions:
            try:
                results = pred.results.predictions
                for result in results:
                    for gp in result.models.language.grouped_predictions:
                        for ep in gp.predictions:
                            hmap = {e.name: e.score for e in ep.emotions}
                            enriched["urgency_score"] = min(
                                max(base["urgency_score"],
                                    hmap.get("distress", 0) + hmap.get("determination", 0)), 1.0)
                            enriched["suspicion_score"] = min(
                                max(base["suspicion_score"],
                                    hmap.get("doubt", 0) + hmap.get("fear", 0)), 1.0)
                            enriched["trust_score"] = min(
                                max(base["trust_score"],
                                    hmap.get("calmness", 0) + hmap.get("contentment", 0)), 1.0)
                            enriched["negotiation_pressure"] = min(
                                max(base["negotiation_pressure"],
                                    hmap.get("anger", 0) + hmap.get("contempt", 0)), 1.0)
            except Exception:
                pass

        save_emotion(uid, enriched, conv_id)
        log.debug("[HUME] enriched uid=%s emotion=%s", uid, enriched["dominant_emotion"])

    except asyncio.TimeoutError:
        log.warning("[HUME_FALLBACK] uid=%s timeout — using rule-based emotion", uid)
    except Exception as e:
        err = str(e).lower()
        if any(k in err for k in ("429", "rate", "limit", "quota", "unauthorized", "403")):
            _hume_rate_limit_until = _time.time() + _HUME_COOLDOWN_S
            log.warning("[HUME_FALLBACK] uid=%s rate-limited/auth — cooldown %ds | %s",
                        uid, _HUME_COOLDOWN_S, e)
        elif "504" in err or "gateway" in err:
            log.warning("[HUME_FALLBACK] uid=%s gateway timeout — using rule-based emotion", uid)
        else:
            log.error("[HUME_FALLBACK] uid=%s enrichment error: %s", uid, e)


# ─── Main entry point ─────────────────────────────────────────────────────────

async def analyze(uid: int, text: str, conv_id: int = None) -> dict:
    """
    Analyze emotion. Returns rule-based result immediately.
    Schedules Hume enrichment in background (when enabled).
    """
    result = analyze_text(text)
    save_emotion(uid, result, conv_id)

    if ENABLE_HUME_EMOTION and HUME_API_KEY:
        asyncio.create_task(_hume_enrich(uid, text, result, conv_id))

    return result
