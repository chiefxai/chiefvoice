"""Deterministic evidence checks for caregiver attendance calls.

The extractor is allowed to propose field values. This module accepts a proposal
only when its cited utterance is a caller turn and the fact can be matched to that
utterance and, where configured, to the assignment.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

_FIELDS = (
    "caregiver_identity",
    "patient_identity",
    "patient_address",
    "attendance_status",
    "attendance_time",
    "caregiver_confirmation",
)
_EXPECTED_KEYS = {
    "caregiver_identity": ("caregiver_identity", "caregiver_name", "expected_caregiver"),
    "patient_identity": ("patient_identity", "patient_name", "expected_patient"),
    "patient_address": ("patient_address", "address", "expected_address"),
}
_GARBLED = re.compile(
    r"^(?:woher|[?!\.\s]+|uh+|um+|garbled|unintelligible|noise|"
    r"கேட்கவில்லை|புரியவில்லை|പറയാൻ കഴിയില്ല|മനസ്സിലായില്ല|తెలియదు|అర్థం కాలేదు)$",
    re.I,
)
_ARRIVAL = (
    "arrived",
    "reached",
    "at the location",
    "here now",
    "i am here",
    "we are here",
    "இங்கே வந்த",
    "வந்துவிட்ட",
    "என் இடத்துக்கு வந்த",
    "എത്തി",
    "വന്നെത്തി",
    "వచ్చాను",
    "చేరుకున్న",
)
_DEPARTURE = (
    "left",
    "departed",
    "leaving now",
    "finished and left",
    "புறப்பட்ட",
    "கிளம்பி",
    "പോയി",
    "പുറപ്പെട്ടു",
    "వెళ్లాను",
    "బయలుదేర",
)
_NOT_ARRIVED = (
    "not arrived",
    "haven't arrived",
    "have not arrived",
    "வரவில்லை",
    "ഇതുവരെ എത്തിയില്ല",
    "ఇంకా రాలేదు",
)
_CONFIRM = ("yes", "confirm", "correct", "ஆம்", "கட்டாயம்", "சரி", "അതെ", "ശരി", "అవును", "సరే")
_SUMMARY_WORDS = (
    "summary",
    "recap",
    "confirm these details",
    "சுருக்கம்",
    "சுருக்கமாக",
    "ചുരുക്കം",
    "సారాంశం",
)


def expected_attendance_assignment(context: Any) -> dict[str, Any]:
    """Select only attendance expectation fields from possibly nested assignment data."""
    result: dict[str, Any] = {}
    aliases = {
        "caregiver_identity": "caregiver_identity",
        "caregiver_name": "caregiver_identity",
        "expected_caregiver": "caregiver_identity",
        "patient_identity": "patient_identity",
        "patient_name": "patient_identity",
        "expected_patient": "patient_identity",
        "patient_address": "patient_address",
        "expected_address": "patient_address",
        "address": "patient_address",
        "patient_locality": "patient_locality",
        "address_match_policy": "address_match_policy",
        "attendance_type": "attendance_type",
    }

    def walk(value: Any, parent: str = "") -> None:
        if not isinstance(value, dict):
            return
        for raw_key, item in value.items():
            key = str(raw_key).casefold()
            canonical = aliases.get(key)
            if canonical and isinstance(item, (str, int, float)) and str(item).strip():
                result.setdefault(canonical, item)
            elif key in {"caregiver", "patient"} and isinstance(item, dict):
                name = item.get("name") or item.get("full_name")
                if name:
                    result.setdefault(
                        "caregiver_identity" if key == "caregiver" else "patient_identity", name
                    )
                address = item.get("address")
                if key == "patient" and address:
                    result.setdefault("patient_address", address)
                walk(item, key)
            elif isinstance(item, dict):
                walk(item, key)

    walk(context)
    return result


_CALLBACK = ("call back", "call me back", "callback", "later", "மீண்டும் அழை", "തിരിച്ചു വിളி", "మళ్లీ కాల్")


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"[\w]+", text, flags=re.UNICODE))


def _similar(left: str, right: str) -> float:
    a, b = _norm(left), _norm(right)
    if not a or not b:
        return 0.0
    if a == b or a in b.split() or b in a.split():
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _mentioned(value: Any, text: str) -> bool:
    value_norm, text_norm = _norm(value), _norm(text)
    if not value_norm or not text_norm:
        return False
    if value_norm in text_norm:
        return True
    pieces = value_norm.split()
    words = text_norm.split()
    if len(pieces) == 1:
        return any(SequenceMatcher(None, pieces[0], word).ratio() >= 0.72 for word in words)
    return all(
        any(SequenceMatcher(None, piece, word).ratio() >= 0.72 for word in words)
        for piece in pieces
    )


def _utterances(transcript: str | None) -> list[tuple[str, str]]:
    result = []
    for line in (transcript or "").splitlines():
        speaker, sep, text = line.partition(":")
        if sep:
            result.append((speaker.strip().casefold(), text.strip()))
    return result


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    normalized = _norm(text)
    return any(_norm(term) and _norm(term) in normalized for term in terms)


def _confirms_final_summary(turns: list[tuple[str, str]], utterance: str) -> bool:
    for index, (speaker, text) in enumerate(turns):
        if speaker not in {"caller", "user"} or _norm(text) != _norm(utterance):
            continue
        prior_assistant = any(
            previous_speaker in {"assistant", "agent"} and _contains(previous_text, _SUMMARY_WORDS)
            for previous_speaker, previous_text in turns[:index]
        )
        return prior_assistant and _contains(text, _CONFIRM)
    return False


def _evidence(field: str, proposal: Any, transcript: str, tool_events: tuple[dict, ...]) -> dict:
    if not isinstance(proposal, dict):
        return {
            "value": None,
            "source": None,
            "original_utterance": None,
            "confidence": 0.0,
            "verification_status": "unverified",
        }
    value = proposal.get("value")
    utterance = str(proposal.get("original_utterance") or proposal.get("utterance") or "")
    confidence = proposal.get("confidence", 0.0)
    confidence = (
        float(confidence)
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
        else 0.0
    )
    source = proposal.get("source")
    status = "unverified"
    caller_turns = [
        text for speaker, text in _utterances(transcript) if speaker in {"caller", "user"}
    ]
    caller_match = next(
        (text for text in caller_turns if utterance and _norm(utterance) in _norm(text)), None
    )
    content_supported = True
    if field in {"caregiver_identity", "patient_identity", "patient_address", "attendance_time"}:
        content_supported = _mentioned(value, caller_match or "")
    if source == "verified_tool_event":
        for event in tool_events:
            args = event.get("arguments", {}) if isinstance(event, dict) else {}
            event_utterance = str(event.get("caller_transcript_at_execution") or "")
            confidence = args.get("confidence") if isinstance(args, dict) else None
            role_ok = (
                isinstance(args, dict)
                and args.get("identity_type") == field
                and args.get("explicitly_stated") is True
                and isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and confidence >= 0.8
            )
            if (
                role_ok
                and _norm(args.get("name")) == _norm(value)
                and _mentioned(value, event_utterance)
                and _norm(utterance)
                and _norm(utterance) in _norm(event_utterance)
                and any(_norm(utterance) in _norm(t) for t in caller_turns)
            ):
                status, source = "verified", "verified_tool_event"
                utterance = next(
                    (turn for turn in caller_turns if _norm(utterance) in _norm(turn)), utterance
                )
                break
    if (
        status != "verified"
        and caller_match is not None
        and content_supported
        and confidence >= 0.65
    ):
        status, source, utterance = "verified", "caller_transcript", caller_match
    return {
        "value": value if status == "verified" else None,
        "source": source if status == "verified" else None,
        "original_utterance": utterance or None,
        "confidence": confidence,
        "verification_status": status,
    }


def validate_attendance(
    *,
    extracted: dict[str, Any],
    transcript: str | None,
    expected: dict[str, Any] | None = None,
    live_claim: dict[str, Any] | None = None,
    tool_events: tuple[dict, ...] = (),
    extraction_status: str = "clear",
) -> dict[str, Any]:
    """Return independently verified attendance evidence and mismatch metadata."""
    expected = expected if isinstance(expected, dict) else {}
    extracted = extracted if isinstance(extracted, dict) else {}
    turns = _utterances(transcript)
    caller_text = " ".join(text for speaker, text in turns if speaker in {"caller", "user"})
    caller_has_garbled_turn = any(
        _GARBLED.fullmatch(_norm(text)) for speaker, text in turns if speaker in {"caller", "user"}
    )
    fields = {
        name: _evidence(name, extracted.get(name), transcript or "", tool_events)
        for name in _FIELDS
    }
    errors: list[str] = []
    mismatches: list[str] = []
    identity_mismatch = False
    for field, keys in _EXPECTED_KEYS.items():
        expected_value = next((expected[key] for key in keys if expected.get(key)), None)
        if (
            field == "patient_address"
            and expected.get("address_match_policy") == "distinctive_locality"
            and expected.get("patient_locality")
        ):
            expected_value = expected["patient_locality"]
        value = fields[field]["value"]
        if expected_value and value:
            threshold = 0.78 if field == "patient_address" else 0.72
            if _similar(str(value), str(expected_value)) < threshold:
                fields[field]["verification_status"] = "mismatch"
                fields[field]["value"] = None
                mismatches.append(field)
                errors.append(f"{field}_does_not_match_assignment")
                identity_mismatch = True

    if extraction_status != "clear" or not caller_text:
        outcome = "unclear"
        errors.append("caller_transcript_unavailable_or_unclear")
    else:
        status_ev = fields["attendance_status"]
        status_text = str(status_ev["original_utterance"] or "")
        status_valid = bool(status_ev["value"] and status_ev["verification_status"] == "verified")
        if status_valid:
            status = _norm(status_ev["value"])
            claim_data_for_direction = (
                (live_claim or {}).get("collected_data") if isinstance(live_claim, dict) else None
            )
            direction = _norm(
                expected.get("attendance_type")
                or (
                    claim_data_for_direction.get("attendance_type", "")
                    if isinstance(claim_data_for_direction, dict)
                    else ""
                )
            )
            check_in = "check in" in direction or "checkin" in direction or "check_in" in direction
            check_out = (
                "check out" in direction or "checkout" in direction or "check_out" in direction
            )
            words = _ARRIVAL if check_in else _DEPARTURE if check_out else _ARRIVAL + _DEPARTURE
            no_arrival = check_in and _contains(status_text, _NOT_ARRIVED)
            action_ok = not no_arrival and _contains(status_text, words)
            if action_ok and (
                (check_in and status in {"not_arrived", "not arrived"})
                or (check_out and status not in {"departed", "left", "checked out", "checked_out"})
            ):
                mismatches.append("attendance_status")
                errors.append("extracted_status_conflicts_with_caller_speech")
            status_conflict = "attendance_status" in mismatches
            if no_arrival:
                if status not in {"not_arrived", "not arrived"}:
                    mismatches.append("attendance_status")
                    errors.append("extracted_status_conflicts_with_caller_speech")
                    status_conflict = True
                outcome = (
                    "unclear"
                    if status_conflict
                    else "callback_required"
                    if _contains(caller_text, _CALLBACK)
                    else "not_arrived"
                )
            elif action_ok:
                time_ev = fields["attendance_time"]
                summary_ev = fields["caregiver_confirmation"]
                identity_ok = all(
                    fields[name]["verification_status"] == "verified"
                    for name in ("caregiver_identity", "patient_identity", "patient_address")
                )
                confirmed = bool(
                    summary_ev["value"] is True
                    and summary_ev["verification_status"] == "verified"
                    and _confirms_final_summary(turns, str(summary_ev["original_utterance"] or ""))
                )
                if status_conflict:
                    outcome = "unclear"
                elif not identity_ok:
                    outcome = "incomplete"
                    errors.append("mandatory_identity_or_address_unverified")
                elif not confirmed:
                    outcome = "incomplete"
                    errors.append("explicit_summary_confirmation_missing")
                else:
                    outcome = "verified"
                if time_ev["verification_status"] != "verified":
                    errors.append("actual_attendance_time_unverified")
            else:
                outcome = "unclear" if _GARBLED.match(_norm(status_text)) else "incomplete"
                errors.append("arrival_or_departure_not_supported_by_caller_speech")
        else:
            outcome = (
                "unclear"
                if caller_has_garbled_turn or _GARBLED.fullmatch(_norm(caller_text))
                else "incomplete"
            )
            errors.append("attendance_status_unverified")

    # End-call claims never override the evidence result.
    claim_data = (live_claim or {}).get("collected_data") if isinstance(live_claim, dict) else None
    claim_outcome = claim_data.get("status") if isinstance(claim_data, dict) else None
    live_reason = (live_claim or {}).get("reason") if isinstance(live_claim, dict) else None
    if isinstance(claim_data, dict):
        expected_direction = expected.get("attendance_type")
        claimed_direction = claim_data.get("attendance_type")
        if (
            expected_direction
            and claimed_direction
            and _norm(expected_direction) != _norm(claimed_direction)
        ):
            mismatches.append("attendance_type")
            errors.append("live_call_attendance_type_claim_conflicts_with_assignment")
        for field in ("caregiver_identity", "patient_identity", "patient_address"):
            claimed = claim_data.get(field)
            verified = fields[field]["value"]
            if claimed and (not verified or _similar(str(claimed), str(verified)) < 0.72):
                mismatches.append(field)
                errors.append(f"live_call_{field}_claim_not_supported")
        claimed_time = claim_data.get(
            "timestamp", claim_data.get("arrival_time", claim_data.get("departure_time"))
        )
        verified_time = fields["attendance_time"]["value"]
        if claimed_time and (not verified_time or _norm(claimed_time) != _norm(verified_time)):
            mismatches.append("attendance_time")
            errors.append("live_call_attendance_time_claim_conflicts")
    claimed_complete = (
        _norm(claim_outcome)
        in {"confirmed", "completed", "checked in", "checked out", "arrived", "departed"}
        or _norm(live_reason) == "completed"
    )
    claimed_early = bool(live_reason and _norm(live_reason) not in {"completed", ""})
    if (claimed_complete and outcome != "verified") or (outcome == "verified" and claimed_early):
        mismatches.append("outcome")
        errors.append("live_call_claim_not_supported_by_verified_evidence")
    if identity_mismatch and extraction_status == "clear":
        outcome = "identity_mismatch"
    return {
        "outcome": outcome,
        "fields": fields,
        "is_consistent": not mismatches,
        "mismatched_fields": sorted(set(mismatches)),
        "validation_errors": sorted(set(errors)),
        "requires_manual_review": outcome == "unclear"
        or bool(mismatches)
        or any(
            fields[name]["verification_status"] in {"unverified", "mismatch"} for name in _FIELDS
        ),
    }
