"""Bộ lập lịch của Hermes — tự đẩy báo cáo qua bot Main Brain (dùng JobQueue của PTB).

- 06:30 mỗi ngày: CHỦ ĐỘNG gợi ý ~3 chủ đề cụ thể cho từng kênh đang chạy (chỉ gợi ý).
- 07:00 mỗi ngày: nhắc hàng đợi (chỉ báo khi sắp hết bài).
- 21:00 mỗi ngày: digest "việc hôm nay" (hàng đợi + comment + đã đăng).
- 20:00 Chủ Nhật: báo cáo tuần + đề xuất chủ đề (Hermes tự viết theo giọng kênh).

Tất cả gửi tới các chat trong HERMES_ALLOWED_CHAT_IDS.
Bố cục báo cáo: VĂN BẢN THUẦN dễ nhìn trên điện thoại, mục có icon mỏ neo, KHÔNG markdown.
"""
import asyncio
import datetime
import logging
import re
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .prompts import build_system

log = logging.getLogger("hermes.jobs")
TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _parse_suggestions(text):
    """Tách output LLM (3 dòng '• 1. <chủ đề> — <lý do>') thành [(title, reason)].

    Fail-open: dòng không khớp -> dùng cả dòng làm title. Bỏ dòng quá ngắn.
    """
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # bỏ bullet + số thứ tự đầu dòng: "• 1. ", "- ", "1) " ...
        line = re.sub(r"^[•\-\*•●\s]*\d*[\.\)]?\s*", "", line).strip()
        if not line:
            continue
        m = re.match(r"^(.+?)\s*[—–\-:]\s+(.+)$", line)
        if m:
            title, reason = m.group(1).strip(), m.group(2).strip()
        else:
            title, reason = line, ""
        if len(title) >= 3:
            out.append((title, reason))
    return out[:5]

# Quy tắc trình bày chung — ép LLM ra báo cáo gọn, dễ nhìn, KHÔNG markdown lỗi.
LAYOUT_RULES = (
    "QUY TẮC TRÌNH BÀY (BẮT BUỘC, ưu tiên cao nhất):\n"
    "- Viết TIẾNG VIỆT. VĂN BẢN THUẦN — TUYỆT ĐỐI KHÔNG dùng dấu * _ ` # hay markdown "
    "(Telegram không parse, sẽ lòi ký tự). Muốn nhấn mạnh thì DÙNG ICON hoặc viết HOA.\n"
    "- Bám ĐÚNG khung mục bên dưới: mỗi mục 1 dòng tiêu đề (kèm icon), rồi 1-3 gạch đầu "
    "dòng bắt đầu bằng '• ', mỗi dòng NGẮN (tối đa ~12 từ).\n"
    "- Số liệu để NỔI BẬT, câu cụt. KHÔNG viết văn xuôi dài, KHÔNG mở bài/kết bài.\n"
    "- Mục nào không có dữ liệu thì ghi đúng 1 dòng: '• — (chưa có)'.\n"
    "- Chỉ dựa trên DỮ LIỆU được đưa, KHÔNG bịa số/không suy diễn.\n"
)


def _post(url, channel):
    if not url:
        return ""
    try:
        with httpx.Client(timeout=60) as c:
            r = c.post(url, json={"channel": channel})
        return r.text.strip() if r.status_code < 400 else ""
    except Exception:  # noqa: BLE001
        return ""


def _chunks(text, size=3500):
    text = text or ""
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class Scheduler:
    def __init__(self, config, llm, memory=None, project_log=None):
        self.config = config
        self.llm = llm
        self.memory = memory
        self.project_log = project_log

    async def _send(self, ctx, text, channel=None):
        for cid in self.config.allowed_chat_ids:
            for chunk in _chunks(text):
                try:
                    await ctx.bot.send_message(cid, chunk)
                except Exception:  # noqa: BLE001
                    log.exception("send_message failed")
            # Lưu báo cáo đã gửi vào bộ nhớ -> Hermes hiểu "báo cáo này/vừa nãy".
            if self.memory is not None and channel is not None:
                try:
                    self.memory.add_report(cid, channel, text)
                except Exception:  # noqa: BLE001
                    log.exception("add_report failed")

    async def _ask(self, system, user):
        return await asyncio.to_thread(
            self.llm.run, system, [], user, [], lambda n, i: ""
        )

    def _today_log(self, channel):
        """Trích các dòng nhật ký dự án HÔM NAY (để biết hôm nay đã đăng gì)."""
        if self.project_log is None:
            return ""
        try:
            raw = self.project_log.read() or ""
        except Exception:  # noqa: BLE001
            return ""
        today = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
        d2 = datetime.datetime.now(TZ).strftime("%d/%m/%Y")
        lines = [ln for ln in raw.splitlines() if (today in ln or d2 in ln)]
        return "\n".join(lines[-15:])

    # 06:30 — Hermes CHỦ ĐỘNG gợi ý chủ đề mỗi sáng (CHỈ gợi ý, KHÔNG tự chốt)
    async def daily_topic_suggest(self, ctx):
        date_str = datetime.datetime.now(TZ).strftime("%d/%m/%Y")
        for key, ch in self.config.channels.items():
            q = await asyncio.to_thread(_post, self.config.queue_url, key)
            if "CHƯA KÍCH HOẠT" in q:
                continue  # kênh chưa active -> không gợi ý
            # Book-first cho Món Quà; kênh khác bám định vị trong persona (channels.yaml).
            book = ch.get("sach_hien_tai") or (
                "Hiểu Về Trái Tim (Minh Niệm)" if key == "mon_qua" else ""
            )
            book_rule = ""
            if key == "mon_qua" and book:
                book_rule = (
                    f"\n- ƯU TIÊN HƯỚNG SÁCH (book-first): rút mỗi chủ đề từ MỘT ý/thông điệp "
                    f"trong cuốn '{book}'. Nếu cạn ý hoặc không hợp, gợi ý chủ đề chữa lành "
                    f"cảm xúc chung."
                )
            log_recent = self._today_log(key)  # nhẹ, fail-open (biết hôm nay đã làm gì)
            system = build_system(ch, [], "") + (
                "\n\nNHIỆM VỤ LÚC NÀY: ĐỀ XUẤT chủ đề cho HÔM NAY (CHỈ gợi ý — TUYỆT ĐỐI "
                "không gọi công cụ, KHÔNG viết script đầy đủ).\n" + LAYOUT_RULES +
                "\nYÊU CẦU RIÊNG:\n"
                "- Đưa ĐÚNG 3 chủ đề CỤ THỂ, đúng định hướng & quy tắc kênh.\n"
                "- Mỗi chủ đề 1 dòng dạng: '• 1. <tên chủ đề ngắn ≤12 từ> — <1 câu lý do rất "
                "ngắn>'.\n"
                "- TRÁNH trùng các chủ đề đang trong HÀNG ĐỢI (liệt kê bên dưới)." + book_rule +
                "\n- KHÔNG mở bài/kết bài, chỉ đúng 3 dòng chủ đề."
            )
            user = (
                f"Kênh {ch['name']}. Ngày {date_str}.\n"
                f"[HÀNG ĐỢI HIỆN TẠI — tránh trùng các chủ đề này]\n{q}\n\n"
                f"[NHẬT KÝ HÔM NAY]\n{log_recent or '(chưa có)'}"
            )
            try:
                text, _ = await self._ask(system, user)
            except Exception as e:  # noqa: BLE001
                log.exception("topic suggest llm error")
                text = "• — (chưa tạo được gợi ý hôm nay)"
            topics = _parse_suggestions(text)
            header = (
                f"🌅 GỢI Ý CHỦ ĐỀ HÔM NAY — {ch['name']}\n"
                f"📅 {date_str}\n\n"
                "👉 Bấm nút \"✅ Chốt chủ đề này\" dưới mỗi gợi ý — Hermes sẽ tự viết script "
                "+ tạo nháp để hệ dựng. (Hoặc nhắn: chốt: <tên chủ đề>.)"
            )
            await self._send(ctx, header, channel=key)  # ghi report cho digest
            if topics:
                # Mỗi chủ đề = 1 tin riêng + 1 nút. callback_data NGẮN (chỉ kênh);
                # tên chủ đề lấy lại từ text của tin khi bấm (callback_query.message.text).
                markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✅ Chốt chủ đề này",
                                           callback_data=f"sugtopic|{key}")]]
                )
                for idx, (title, reason) in enumerate(topics, 1):
                    body = f"📌 {title}"
                    if reason:
                        body += f"\n💡 {reason}"
                    for cid in self.config.allowed_chat_ids:
                        try:
                            await ctx.bot.send_message(
                                cid, body, reply_markup=markup
                            )
                        except Exception:  # noqa: BLE001
                            log.exception("gửi nút gợi ý lỗi")
            else:
                # Fail-open: không tách được -> gửi nguyên văn như cũ (không nút).
                await self._send(
                    ctx,
                    text + "\n\n👉 nhắn: chốt: <tên chủ đề> để Hermes tạo nháp.",
                    channel=key,
                )

    # 07:00 — nhắc hàng đợi (chỉ khi sắp hết bài)
    async def daily_reminder(self, ctx):
        for key, ch in self.config.channels.items():
            q = await asyncio.to_thread(_post, self.config.queue_url, key)
            if "CHƯA KÍCH HOẠT" in q:
                continue  # kênh chưa active -> bỏ qua, không nhắc
            if "⚠️" in q:
                text = (
                    f"⏰ NHẮC HÀNG ĐỢI — {ch['name']}\n"
                    f"📥 Tình trạng kho video\n{q}\n\n"
                    "💡 Nên dựng thêm video để không gián đoạn lịch đăng."
                )
                await self._send(ctx, text, channel=key)

    # 21:00 — tổng kết việc trong ngày
    async def daily_digest(self, ctx):
        date_str = datetime.datetime.now(TZ).strftime("%d/%m/%Y")
        for key, ch in self.config.channels.items():
            q = await asyncio.to_thread(_post, self.config.queue_url, key)
            if "CHƯA KÍCH HOẠT" in q:
                # Kênh chưa active -> báo gọn, KHÔNG lấy dữ liệu kênh khác.
                await self._send(
                    ctx,
                    f"🌙 TỔNG KẾT HÔM NAY — {ch['name']}\n📅 {date_str}\n\n"
                    "⏸️ Kênh chưa kích hoạt — chưa có hoạt động để tổng kết.",
                    channel=key,
                )
                continue
            cm = await asyncio.to_thread(_post, self.config.comment_url, key)
            posted = self._today_log(key)
            system = (
                "Bạn là Hermes — quản lý vận hành kênh. Viết BÁO CÁO TỔNG KẾT CUỐI NGÀY "
                "cho điện thoại.\n" + LAYOUT_RULES +
                "\nKHUNG BÁO CÁO (giữ nguyên thứ tự, icon và tên mục):\n"
                f"🌙 TỔNG KẾT HÔM NAY — {ch['name']}\n"
                f"📅 {date_str}\n"
                "📊 Đăng hôm nay\n• ...\n"
                "📥 Hàng đợi\n• Sẵn sàng: N | Nháp: N | Đã đăng: N\n"
                "💬 Tương tác\n• ...\n"
                "⚠️ Cần làm\n• ...\n"
                "💡 Gợi ý mai\n• ...\n"
                "\nVÍ DỤ MẪU (chỉ để học BỐ CỤC, đừng chép số):\n"
                f"🌙 TỔNG KẾT HÔM NAY — {ch['name']}\n"
                f"📅 {date_str}\n"
                "📊 Đăng hôm nay\n"
                "• YouTube + Facebook: 1 video ✅\n"
                "📥 Hàng đợi\n"
                "• Sẵn sàng: 2 ⚠️ | Nháp: 0 | Đã đăng: 3\n"
                "💬 Tương tác\n"
                "• Bình luận mới: 0\n"
                "⚠️ Cần làm\n"
                "• Dựng thêm video (sắp hết bài sẵn sàng)\n"
                "💡 Gợi ý mai\n"
                "• Chốt 1 chủ đề mới cho hàng đợi"
            )
            user = (
                f"Kênh {ch['name']}. Ngày {date_str}.\n"
                f"[ĐÃ ĐĂNG HÔM NAY - nhật ký, chỉ lấy mục đăng bài của kênh này hôm nay]\n"
                f"{posted or '(không có ghi nhận đăng hôm nay)'}\n\n"
                f"[HÀNG ĐỢI]\n{q}\n\n[COMMENT GẦN ĐÂY]\n{cm}"
            )
            try:
                text, _ = await self._ask(system, user)
            except Exception as e:  # noqa: BLE001
                log.exception("digest llm error")
                text = (
                    f"🌙 TỔNG KẾT HÔM NAY — {ch['name']}\n📅 {date_str}\n\n"
                    f"📥 Hàng đợi\n{q}\n\n(Không tạo được tổng kết: {e})"
                )
            await self._send(ctx, text, channel=key)

    # 20:00 Chủ Nhật — báo cáo tuần + đề xuất chủ đề
    async def weekly_report(self, ctx):
        if datetime.datetime.now(TZ).weekday() != 6:  # 6 = Chủ Nhật
            return
        date_str = datetime.datetime.now(TZ).strftime("%d/%m/%Y")
        for key, ch in self.config.channels.items():
            hq = await asyncio.to_thread(_post, self.config.hieuqua_url, key)
            q = await asyncio.to_thread(_post, self.config.queue_url, key)
            if "CHƯA KÍCH HOẠT" in q:
                await self._send(
                    ctx,
                    f"📊 BÁO CÁO TUẦN — {ch['name']}\n📅 {date_str}\n\n"
                    "⏸️ Kênh chưa kích hoạt — chưa có dữ liệu để báo cáo.",
                    channel=key,
                )
                continue
            system = build_system(ch, [], "") + (
                "\n\nNHIỆM VỤ LÚC NÀY: viết BÁO CÁO TUẦN cho kênh.\n" + LAYOUT_RULES +
                "\nKHUNG BÁO CÁO (giữ nguyên thứ tự, icon và tên mục):\n"
                f"📊 BÁO CÁO TUẦN — {ch['name']}\n"
                f"📅 {date_str}\n"
                "📈 Hiệu quả tuần\n• ...\n"
                "📥 Hàng đợi\n• Sẵn sàng: N | Nháp: N | Đã đăng: N\n"
                "✅ Nên làm tuần tới\n• ...\n"
                "💡 7 chủ đề gợi ý\n• 1. ...\n• 2. ...\n(đủ 7, mỗi dòng 1 chủ đề ngắn, "
                "đúng định hướng & quy tắc kênh)\n"
            )
            user = f"[HIỆU QUẢ TUẦN]\n{hq}\n\n[HÀNG ĐỢI]\n{q}"
            try:
                text, _ = await self._ask(system, user)
            except Exception as e:  # noqa: BLE001
                log.exception("weekly llm error")
                text = (
                    f"📊 BÁO CÁO TUẦN — {ch['name']}\n📅 {date_str}\n\n"
                    f"📈 Hiệu quả\n{hq}\n\n(Không tạo được báo cáo: {e})"
                )
            await self._send(ctx, text, channel=key)


def register(application, config, llm, memory=None, project_log=None):
    """Đăng ký các job vào JobQueue. Bỏ qua nếu chưa có người nhận hoặc job_queue."""
    if not config.allowed_chat_ids:
        log.warning("HERMES_ALLOWED_CHAT_IDS trống — không đăng ký báo cáo tự động.")
        return
    jq = getattr(application, "job_queue", None)
    if jq is None:
        log.warning("JobQueue không khả dụng — bỏ qua lập lịch.")
        return
    s = Scheduler(config, llm, memory, project_log)
    jq.run_daily(s.daily_topic_suggest, time=datetime.time(6, 30, tzinfo=TZ))
    jq.run_daily(s.daily_reminder, time=datetime.time(7, 0, tzinfo=TZ))
    jq.run_daily(s.daily_digest, time=datetime.time(21, 0, tzinfo=TZ))
    jq.run_daily(s.weekly_report, time=datetime.time(20, 0, tzinfo=TZ))
    log.info("Đã đăng ký 4 báo cáo tự động (06:30 gợi ý chủ đề, 07:00, 21:00, CN 20:00).")
