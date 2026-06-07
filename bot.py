"""Telegram bot RIÊNG cho Hermes. Chạy ở chế độ long-polling (không đụng bot MQ/CKT của n8n)."""
import asyncio
import base64
import logging

from .prompts import build_system
from .tools import tool_specs, ToolExecutor

log = logging.getLogger("hermes.bot")


def _chunks(text, size=4000):
    text = text or "(trống)"
    return [text[i:i + size] for i in range(0, len(text), size)]


def _extract_topic(text):
    """Lấy tên chủ đề từ tin gợi ý (dòng bắt đầu bằng 📌). Fallback: dòng đầu."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("📌"):
            return ln.lstrip("📌").strip(" -—:•").strip()
    return lines[0].strip(" -—:•").strip() if lines else ""


class HermesBot:
    def __init__(self, config, memory, llm, project_log=None):
        self.config = config
        self.memory = memory
        self.llm = llm
        self.project_log = project_log

    def _ok(self, chat_id):
        ids = self.config.allowed_chat_ids
        return (not ids) or (chat_id in ids)

    async def start(self, update, ctx):
        cid = update.effective_chat.id
        if not self._ok(cid):
            return
        key = self.memory.get_channel(cid, self.config.default_channel)
        await update.message.reply_text(
            "Chào Mr Rise 👋 Hermes đã sẵn sàng.\n"
            f"Kênh đang phụ trách: {self.config.channel(key)['name']}.\n\n"
            "Lệnh: /mq (Món Quà) · /ckt (Chạm Kinh Thánh) · /kenh · /quen\n"
            "Thử: \"gợi ý 5 chủ đề\" hoặc \"chốt: <tên chủ đề>\"."
        )

    async def kenh(self, update, ctx):
        cid = update.effective_chat.id
        if not self._ok(cid):
            return
        key = self.memory.get_channel(cid, self.config.default_channel)
        await update.message.reply_text(
            f"Đang phụ trách kênh: {self.config.channel(key)['name']}"
        )

    async def _switch(self, update, key):
        cid = update.effective_chat.id
        if not self._ok(cid):
            return
        if not self.config.channel(key):
            await update.message.reply_text("Không tìm thấy kênh.")
            return
        self.memory.set_channel(cid, key)
        await update.message.reply_text(
            f"Đã chuyển sang kênh: {self.config.channel(key)['name']}"
        )

    async def mq(self, update, ctx):
        await self._switch(update, "mon_qua")

    async def ckt(self, update, ctx):
        await self._switch(update, "cham_kinh_thanh")

    async def quen(self, update, ctx):
        cid = update.effective_chat.id
        if not self._ok(cid):
            return
        key = self.memory.get_channel(cid, self.config.default_channel)
        self.memory.clear_messages(cid, key)
        await update.message.reply_text(
            "Đã xoá lịch sử trò chuyện của kênh này (ghi nhớ dài hạn vẫn giữ)."
        )

    async def _process(self, update, ctx, user_content, mem_user_text):
        """Lõi chung cho cả tin chữ lẫn tin ảnh.

        user_content: str hoặc list block (đa phương tiện) gửi cho LLM lượt này.
        mem_user_text: bản chữ để lưu vào lịch sử (ảnh lưu dạng placeholder).
        """
        cid = update.effective_chat.id
        key = self.memory.get_channel(cid, self.config.default_channel)
        ch = self.config.channel(key)
        log_text = self.project_log.read() if self.project_log else ""
        system = build_system(ch, self.memory.notes(cid, key), log_text)
        # Nạp các BÁO CÁO TỰ ĐỘNG gần đây Hermes đã gửi -> hiểu "báo cáo này/vừa nãy".
        try:
            reps = self.memory.recent_reports(cid, limit=4)
        except Exception:  # noqa: BLE001
            reps = []
        if reps:
            system += (
                "\n\n[CÁC BÁO CÁO TỰ ĐỘNG GẦN ĐÂY BẠN (Hermes) ĐÃ TỰ GỬI cho người dùng — "
                "dùng để hiểu khi họ nói 'báo cáo này / vừa nãy / các báo cáo'. Nếu họ yêu cầu "
                "chỉnh BỐ CỤC/cách trình bày các báo cáo này, hãy hiểu là họ muốn đổi cách Hermes "
                "trình bày báo cáo từ lần sau và phản hồi cụ thể — ĐỪNG đòi họ gửi lại file/ảnh.]\n"
                + "\n———\n".join(reps)
            )
        history = self.memory.recent(cid, key, limit=20)
        executor = ToolExecutor(self.config, key, cid, self.memory, self.project_log)

        try:
            await ctx.bot.send_chat_action(cid, "typing")
        except Exception:  # noqa: BLE001
            pass

        try:
            reply, _used = await asyncio.to_thread(
                self.llm.run, system, history, user_content, tool_specs(), executor
            )
        except Exception as e:  # noqa: BLE001
            log.exception("LLM error")
            await update.message.reply_text(f"Hermes gặp lỗi khi xử lý: {e}")
            return

        self.memory.add_message(cid, key, "user", mem_user_text)
        self.memory.add_message(cid, key, "assistant", reply)
        for c in _chunks(reply):
            await update.message.reply_text(c)

        # Gửi ảnh do tool tao_anh tạo ra (nếu có) qua Telegram.
        for img_bytes, cap in getattr(executor, "pending_images", []):
            try:
                await ctx.bot.send_photo(cid, photo=bytes(img_bytes), caption=(cap or None))
            except Exception as e:  # noqa: BLE001
                log.exception("send_photo error")
                await update.message.reply_text(f"Tạo ảnh xong nhưng gửi lỗi: {e}")

    async def on_text(self, update, ctx):
        cid = update.effective_chat.id
        if not self._ok(cid):
            await update.message.reply_text("Bạn không có quyền dùng Hermes.")
            return
        text = update.message.text or ""
        # Nếu người dùng TRẢ LỜI (reply) vào một tin nhắn nào đó (vd báo cáo Hermes
        # đã gửi), nạp nội dung tin được trả lời vào ngữ cảnh để Hermes hiểu "tin này".
        quoted = ""
        rtm = getattr(update.message, "reply_to_message", None)
        if rtm is not None:
            quoted = (getattr(rtm, "text", None) or getattr(rtm, "caption", None) or "").strip()
        if quoted:
            user_content = (
                "[NGỮ CẢNH — người dùng đang TRẢ LỜI vào tin nhắn dưới đây "
                "(thường là một báo cáo bạn vừa gửi)]\n"
                + quoted
                + "\n[HẾT NGỮ CẢNH]\n\nNgười dùng nói: " + text
            )
            snippet = quoted.replace("\n", " ")[:80]
            mem_text = text + "  ⟵(trả lời vào: " + snippet + "…)"
        else:
            user_content = text
            mem_text = text
        await self._process(update, ctx, user_content, mem_text)

    async def on_callback(self, update, ctx):
        """Xử lý nút inline. Hiện hỗ trợ: 'sugtopic|<channel>' = chốt chủ đề gợi ý 06:30.

        Nút nằm trên tin của CHÍNH bot Hermes (job daily_topic_suggest gửi), nên callback
        về đây (chỉ Hermes poll bot Hermes). KHÔNG đụng bot MQ/CKT của n8n.
        """
        q = update.callback_query
        if q is None:
            return
        cid = q.message.chat.id if q.message else q.from_user.id
        if not self._ok(cid):
            try:
                await q.answer("Bạn không có quyền.")
            except Exception:  # noqa: BLE001
                pass
            return
        data = q.data or ""
        if not data.startswith("sugtopic|"):
            try:
                await q.answer()  # nút lạ -> bỏ qua êm
            except Exception:  # noqa: BLE001
                pass
            return
        key = data.split("|", 1)[1].strip()
        ch = self.config.channel(key)
        topic = _extract_topic(q.message.text if q.message else "")
        if not ch or not topic:
            try:
                await q.answer("Không đọc được chủ đề/kênh.")
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            await q.answer("Đang viết script + tạo nháp…")
        except Exception:  # noqa: BLE001
            pass
        # Gỡ nút để tránh bấm trùng (chốt 2 lần).
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass

        # Chạy luồng CHỐT cho ĐÚNG kênh ghi trong nút (không theo kênh đang chọn).
        log_text = self.project_log.read() if self.project_log else ""
        system = build_system(ch, self.memory.notes(cid, key), log_text)
        executor = ToolExecutor(
            self.config, key, cid, self.memory, self.project_log
        )
        directive = (
            f'Hãy CHỐT ngay chủ đề sau cho kênh "{ch["name"]}": "{topic}".\n'
            "BẮT BUỘC gọi công cụ chot_chu_de với script ĐẦY ĐỦ (KHỐI 1/2/3 + tiêu đề/"
            "mô tả/caption) đúng phong cách & quy tắc kênh. Không hỏi lại, không gợi ý "
            "thêm — chốt luôn để hệ tự dựng."
        )
        try:
            await ctx.bot.send_chat_action(cid, "typing")
        except Exception:  # noqa: BLE001
            pass
        try:
            reply, _used = await asyncio.to_thread(
                self.llm.run, system, [], directive, tool_specs(), executor
            )
        except Exception as e:  # noqa: BLE001
            log.exception("on_callback chot error")
            await ctx.bot.send_message(cid, f"Chốt chủ đề lỗi: {e}")
            return
        self.memory.add_message(cid, key, "user", f"[nút] chốt: {topic}")
        self.memory.add_message(cid, key, "assistant", reply)
        for c in _chunks("✅ Đã chốt: " + topic + "\n\n" + reply):
            await ctx.bot.send_message(cid, c)

    async def on_photo(self, update, ctx):
        cid = update.effective_chat.id
        if not self._ok(cid):
            await update.message.reply_text("Bạn không có quyền dùng Hermes.")
            return
        caption = update.message.caption or "Xem giúp mình tấm ảnh này nhé."
        try:
            photo = update.message.photo[-1]  # bản phân giải cao nhất
            tgfile = await ctx.bot.get_file(photo.file_id)
            buf = await tgfile.download_as_bytearray()
        except Exception as e:  # noqa: BLE001
            await update.message.reply_text(f"Không tải được ảnh: {e}")
            return
        b64 = base64.b64encode(bytes(buf)).decode("ascii")
        content = [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": caption},
        ]
        await self._process(update, ctx, content, f"[đã gửi 1 ảnh] {caption}")
