"""Điểm khởi chạy Hermes."""
import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from hermes.config import Config
from hermes.memory import Memory
from hermes.llm import LLM
from hermes.bot import HermesBot
from hermes.projectlog import ProjectLog
from hermes import jobs


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    log = logging.getLogger("hermes")

    cfg = Config()
    mem = Memory(cfg.db_path)
    llm = LLM(cfg.vertex_base_url, cfg.vertex_api_key, cfg.model)
    plog = ProjectLog(cfg.log_write_url, cfg.log_read_url)
    bot = HermesBot(cfg, mem, llm, plog)

    app = Application.builder().token(cfg.bot_token).build()
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("kenh", bot.kenh))
    app.add_handler(CommandHandler("mq", bot.mq))
    app.add_handler(CommandHandler("ckt", bot.ckt))
    app.add_handler(CommandHandler("quen", bot.quen))
    app.add_handler(MessageHandler(filters.PHOTO, bot.on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))
    # Nút inline "✅ Chốt chủ đề" trên tin gợi ý 06:30 (callback về chính bot Hermes).
    app.add_handler(CallbackQueryHandler(bot.on_callback))

    jobs.register(app, cfg, llm, mem, plog)

    log.info("Hermes khởi động. Kênh mặc định: %s. Số kênh: %d",
             cfg.default_channel, len(cfg.channels))
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
