"""Shared localized completion phrases for auto-ending live calls."""

_COMPLETION_PHRASES = {
    "en": "That is all the information I needed. Thank you for your time. Goodbye.",
    "hi": "मुझे सभी ज़रूरी जानकारी मिल गई है। आपका समय देने के लिए धन्यवाद। नमस्ते।",
    "ml": "എനിക്ക് ആവശ്യമായ എല്ലാ വിവരങ്ങളും ലഭിച്ചു. നിങ്ങളുടെ സമയത്തിന് നന്ദി. നമസ്കാരം.",
    "ta": "நான் கேட்ட தகவல்கள் அனைத்தும் கிடைத்துவிட்டன. உங்கள் நேரத்திற்கு நன்றி. வணக்கம்.",
    "te": "నాకు అవసరమైన సమాచారం అంతా లభించింది. మీ సమయానికి ధన్యవాదాలు. నమస్కారం.",
}


def completion_phrase_for_language(language: str | None) -> str | None:
    """Return a localized final sentence, or None when the language is unknown."""
    if not language:
        return None
    language_code = language.strip().lower().split("-", maxsplit=1)[0]
    return _COMPLETION_PHRASES.get(language_code)


def completion_marker_for_language(language: str | None) -> str | None:
    """Return the localized completion marker, or None when the language is unknown."""
    if not language:
        return None
    language_code = language.strip().lower().split("-", maxsplit=1)[0]
    markers = {
        "en": "that is all the information i needed",
        "hi": "मुझे सभी ज़रूरी जानकारी मिल गई है",
        "ml": "എനിക്ക് ആവശ്യമായ എല്ലാ വിവരങ്ങളും ലഭിച്ചു",
        "ta": "நான் கேட்ட தகவல்கள் அனைத்தும் கிடைத்துவிட்டன",
        "te": "నాకు అవసరమైన సమాచారం అంతా లభించింది",
    }
    return markers.get(language_code)
