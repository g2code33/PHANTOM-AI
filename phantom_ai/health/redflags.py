"""Health safety layer (red-flag system).

If the user reports potentially serious symptoms or measurements, the Health
brain must recognize that the situation may require urgent professional
assessment and respond accordingly:
1. Clearly state the information may be concerning.
2. Never give false reassurance.
3. Encourage appropriate urgent/emergency care when warranted.
4. Never attempt to manage a potentially life-threatening situation alone.
5. Never delay emergency care for lengthy analysis.

Safety always outranks conversational completeness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Symptoms that warrant urgent/emergency care. Keys are matched as substrings
# against the user's own recorded symptom text.
EMERGENCY_SYMPTOMS: dict[str, str] = {
    "chest pain": "could indicate a heart problem (e.g. heart attack) — call emergency services now",
    "chest pressure": "could indicate a heart problem — call emergency services now",
    "difficulty breathing": "could be a serious breathing problem — seek emergency care now",
    "shortness of breath": "when severe/sudden can be serious — seek emergency care now",
    "cannot breathe": "seek emergency care immediately",
    "unable to breathe": "seek emergency care immediately",
    "seizure": "seek emergency care — do not drive",
    "stroke": "seek emergency care immediately",
    "numbness on one side": "could be a sign of stroke — call emergency services now",
    "slurred speech": "could be a sign of stroke — call emergency services now",
    "face drooping": "could be a sign of stroke — call emergency services now",
    "loss of consciousness": "seek emergency care immediately",
    "fainted": "seek emergency care if you lost consciousness",
    "passed out": "seek emergency care immediately",
    "severe bleeding": "apply pressure and call emergency services now",
    "uncontrolled bleeding": "call emergency services now",
    "suicidal": "you deserve immediate help — call emergency services or a crisis line now",
    "self-harm": "please get help now — emergency services or a crisis line",
    "hurt myself": "please get help now",
    "severe allergic reaction": "call emergency services now (possible anaphylaxis)",
    "swelling of the face": "could be a serious allergic reaction — seek emergency care now",
    "swelling of the lips": "could be anaphylaxis — seek emergency care now",
    "swelling of the throat": "could block your airway — call emergency services now",
    "vomiting blood": "seek emergency care now",
    "blood in vomit": "seek emergency care now",
    "black stools": "can indicate internal bleeding — seek urgent medical care",
    "high fever with stiff neck": "can be serious (e.g. meningitis) — seek urgent care now",
    "severe headache worst of my life": "seek urgent medical evaluation now",
    "sudden severe headache": "seek urgent medical evaluation",
    "sudden blurred vision": "seek urgent medical evaluation",
    "sudden vision loss": "seek urgent medical evaluation now",
    "severe abdominal pain": "seek urgent medical evaluation",
    "crushing": "could indicate a heart problem — call emergency services now",
    "drowning": "call emergency services immediately",
}

# Symptoms that clearly warrant a prompt (non-emergency) professional consult.
CONSULT_SYMPTOMS: set[str] = {
    "persistent", "worsening", "recurring", "unexplained weight loss",
    "fever for more than", "prolonged", "blood in urine", "blood in stool",
    "jaundice", "yellowing", "severe fatigue", "night sweats", "lumps",
    "chest pain with activity", "heart palpitations", "irregular heartbeat",
    "pain radiating", "tingling in", "numbness", "weakness",
}

# Measurement thresholds → level + message. value_lt / value_gt compare the
# parsed number; the message always recommends professional assessment, never
# a diagnosis.
MEASUREMENT_RULES: dict[str, dict[str, Any]] = {
    "spo2": {"unit": "%", "lt": 92, "level": "emergency",
             "message": "blood oxygen below 92% can be serious — seek urgent medical care"},
    "heart_rate": {"unit": "bpm", "gt": 130, "level": "warning",
                   "message": "resting heart rate above 130 bpm warrants medical review"},
    "heart_rate_low": {"unit": "bpm", "lt": 40, "level": "warning",
                       "message": "heart rate below 40 bpm warrants medical review"},
    "systolic": {"unit": "mmHg", "gt": 180, "level": "warning",
                 "message": "systolic pressure above 180 mmHg warrants urgent medical review"},
    "systolic_low": {"unit": "mmHg", "lt": 90, "level": "warning",
                     "message": "systolic pressure below 90 mmHg warrants medical review"},
    "diastolic": {"unit": "mmHg", "gt": 110, "level": "warning",
                  "message": "diastolic pressure above 110 mmHg warrants medical review"},
    "glucose": {"unit": "mg/dL", "gt": 300, "level": "warning",
                "message": "blood glucose above 300 mg/dL warrants medical review"},
    "glucose_low": {"unit": "mg/dL", "lt": 55, "level": "warning",
                    "message": "blood glucose below 55 mg/dL is dangerous — treat and seek medical help"},
    "temperature": {"unit": "°C", "gt": 39.5, "level": "warning",
                    "message": "temperature above 39.5°C warrants medical review"},
    "temperature_low": {"unit": "°C", "lt": 35, "level": "warning",
                        "message": "temperature below 35°C (hypothermia) warrants urgent care"},
}


@dataclass
class RedFlag:
    level: str                      # info | warning | emergency
    message: str
    advice: str = ""
    matched: list[str] = field(default_factory=list)

    @property
    def is_serious(self) -> bool:
        return self.level in ("warning", "emergency")

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "message": self.message,
                "advice": self.advice, "matched": self.matched}


def check_symptom(text: str, severity: int = 0) -> Optional[RedFlag]:
    """Inspect voluntarily-recorded symptom text for red flags."""
    if not text:
        return None
    lowered = text.lower()
    for marker, message in EMERGENCY_SYMPTOMS.items():
        if marker in lowered:
            return RedFlag(
                level="emergency",
                message=f"The symptom you recorded may be serious: '{marker}'.",
                advice="Do not wait for further analysis. Call your local emergency number "
                       "(or crisis line if this is about self-harm) now, or go to the nearest "
                       "emergency department. I cannot assess urgency from text.",
                matched=[marker])
    for marker in CONSULT_SYMPTOMS:
        if marker in lowered:
            return RedFlag(
                level="warning",
                message=f"What you described ('{marker}') is worth discussing with a healthcare "
                        "professional rather than relying on my summary.",
                advice="Book a consultation with your doctor or a pharmacist/telehealth service "
                       "to review it.",
                matched=[marker])
    if severity >= 8:
        return RedFlag(
            level="warning",
            message="You rated this symptom at the top of the severity scale.",
            advice="Consider contacting a healthcare professional today.",
            matched=["severity>=8"])
    if severity >= 10:
        return RedFlag(
            level="emergency",
            message="You rated this symptom as the most severe possible.",
            advice="Seek emergency medical care now.",
            matched=["severity>=10"])
    return None


def check_measurement(metric: str, value: float, unit: str = "") -> Optional[RedFlag]:
    """Inspect a user-provided measurement against safety thresholds."""
    rule = MEASUREMENT_RULES.get(metric)
    if rule is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    matches: list[str] = []
    if rule.get("gt") is not None and value > rule["gt"]:
        matches.append(f">{rule['gt']}")
    if rule.get("lt") is not None and value < rule["lt"]:
        matches.append(f"<{rule['lt']}")
    if not matches:
        return None
    return RedFlag(
        level=rule["level"],
        message=rule["message"].replace("<", "below ").replace(">", "above "),
        advice="These thresholds are general guides, not a diagnosis. Please have a "
               "professional interpret your reading.",
        matched=[f"{metric} {value} {unit}".strip()])


def explain_red_flag(flag: RedFlag) -> str:
    head = {"info": "ℹ️ Note", "warning": "⚠️ Please take note",
            "emergency": "🚨 URGENT"}[flag.level]
    return f"{head}\n{flag.message}\n\n{flag.advice}"
