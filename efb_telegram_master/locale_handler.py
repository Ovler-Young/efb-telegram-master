# coding=utf-8

import gettext
import logging
from typing import TYPE_CHECKING

from language_tags import tags
from telegram import Update
from telegram.ext import BaseHandler

from .paths import LOCALE_DIR

if TYPE_CHECKING:
    from telegram.ext import Application, CallbackContext
    from . import TelegramChannel


class LocaleHandler(BaseHandler):
    """
    PTB 22-compatible locale updater handler.

    This remains as a small compatibility wrapper for older ETM call sites, even
    though the current runtime primarily wires locale updates through
    ``TelegramChannel.update_locale``.
    """

    def __init__(self, channel: 'TelegramChannel'):
        async def void_callback(update: Update, context: 'CallbackContext'):
            return None

        super().__init__(void_callback, block=False)
        self.logger = logging.getLogger(__name__)
        self.channel = channel
        self.auto_locale = self.channel.flag('auto_locale')

    def check_update(self, update: object):
        if not self.auto_locale:
            return False
        if not isinstance(update, Update):
            return False
        if not update.effective_user or not update.effective_user.language_code:
            return False

        language_code = update.effective_user.language_code
        self.logger.debug("[%s] Update has language %s.", update.update_id, language_code)
        if language_code == self.channel.locale:
            return False

        self.channel.locale = language_code
        tag = tags.tag(language_code)
        if tag.language:
            locale = tag.language.format
            if tag.region:
                locale += "_" + tag.region.format
        else:
            locale = language_code.replace('-', '_')
        self.logger.info("Updating locale to %s", locale)
        self.channel.translator = gettext.translation(
            "efb_telegram_master",
            str(LOCALE_DIR),
            languages=[locale, 'C'],
            fallback=True,
        )
        return False

    async def handle_update(self, update: Update, application: 'Application', check_result: object, context: 'CallbackContext'):
        return None
