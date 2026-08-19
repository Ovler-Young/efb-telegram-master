"""Late-bound translations shared by Telegram channel collaborators."""

from __future__ import annotations

import logging
import os
from gettext import NullTranslations, translation

from language_tags import tags
from telegram import Update

from .paths import LOCALE_DIR


class LocaleState:
    """Hold the translation selected from the most recent Telegram update."""

    def __init__(self) -> None:
        self.locale: str | None = None
        self.translator: NullTranslations = translation("efb_telegram_master", os.fspath(LOCALE_DIR), fallback=True)

    def gettext(self, message: str) -> str:
        return self.translator.gettext(message)

    def ngettext(self, singular: str, plural: str, count: int) -> str:
        return self.translator.ngettext(singular, plural, count)

    def update(self, update: Update, logger: logging.Logger) -> None:
        if not update.effective_user or not update.effective_user.language_code:
            return
        language_code = update.effective_user.language_code
        if language_code == self.locale:
            return
        tag = tags.tag(language_code)
        locale = tag.language.format if tag.language else language_code.replace("-", "_")
        if tag.language and tag.region:
            locale += "_" + tag.region.format
        logger.info("Telegram locale updated", extra={"event": "telegram_channel.locale_updated", "locale": locale})
        self.locale = language_code
        self.translator = translation("efb_telegram_master", os.fspath(LOCALE_DIR), languages=[locale, "C"], fallback=True)
