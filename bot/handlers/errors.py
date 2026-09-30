"""Последняя линия обороны: любая ошибка в обработчике не должна «вешать» кнопку."""

import logging

from aiogram.types import ErrorEvent

log = logging.getLogger(__name__)


# Регистрируется на диспетчере (dp.errors), чтобы ловить ошибки из всех роутеров.
async def on_error(event: ErrorEvent) -> bool:
    log.error("Ошибка при обработке обновления: %r", event.exception, exc_info=event.exception)
    callback = event.update.callback_query
    if callback is not None:
        try:
            await callback.answer(
                "⚠️ Что-то пошло не так. Попробуйте ещё раз; если повторится — "
                "подробности в логах бота.",
                show_alert=True,
            )
        except Exception:
            pass  # на кнопку уже ответили или время ответа истекло
    return True
