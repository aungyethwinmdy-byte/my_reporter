import html


def _button_rows(uploads, drive_urls):
    rows = []
    for filename, link in uploads:
        rows.append([{"text": f"ဖိုင်ဖွင့်ရန် · {filename}", "url": link}])
    for label, link in drive_urls.items():
        rows.append([{"text": f"ဖိုဒါဖွင့်ရန် · {label}", "url": link}])
    return {"inline_keyboard": rows} if rows else None


def build_success_notification(date_label, uploads, drive_urls):
    lines = [
        "<b>✅ လုပ်ငန်းစဉ် အောင်မြင်စွာ ပြီးဆုံးပါပြီ</b>",
        f"<i>📅 ရက်စွဲ — {html.escape(date_label)}</i>",
        "",
        f"<b>📥 အသစ်ရရှိသော စာစောင် — {len(uploads)} ခု</b>",
    ]
    for index, (filename, _) in enumerate(uploads, start=1):
        lines.append(f"<b>{index}.</b> {html.escape(filename)}")
    lines.extend(
        [
            "",
            "<b>အောက်ပါခလုတ်များမှ ဖိုင် သို့မဟုတ် Google Drive ဖိုဒါကို ဖွင့်နိုင်ပါသည်။</b>",
        ]
    )
    return "\n".join(lines), _button_rows(uploads, drive_urls)


def build_error_notification(date_label, errors):
    lines = [
        "<b>⚠️ လုပ်ငန်းစဉ်တွင် သတိပေးချက်ရှိပါသည်</b>",
        f"<i>📅 ရက်စွဲ — {html.escape(date_label)}</i>",
        "",
        "<b>စစ်ဆေးရန်လိုသောအချက်များ</b>",
    ]
    for label, error in errors:
        lines.append(f"• <b>{html.escape(label)}</b> — {html.escape(str(error))}")
    return "\n".join(lines), None


def build_history_notification(history_rows, counts, limit):
    status_labels = {
        "uploaded": "တင်ပြီး",
        "skipped": "ကျော်ထား",
        "failed": "မအောင်မြင်",
    }
    lines = [
        "<b>🗃 DOWNLOAD HISTORY</b>",
        f"<i>နောက်ဆုံးမှတ်တမ်း {len(history_rows)} ခု · ပြသနိုင်သည့်အများဆုံး {limit} ခု</i>",
        "",
        "<b>📊 SUMMARY</b>",
        f"• စုစုပေါင်း — {counts['total']}",
        f"• တင်ပြီး — {counts['uploaded']}",
        f"• ကျော်ထား — {counts['skipped']}",
        f"• မအောင်မြင် — {counts['failed']}",
        "",
    ]
    buttons = []
    if not history_rows:
        lines.append("မှတ်တမ်းမရှိသေးပါ။")
        return "\n".join(lines), None

    for index, row in enumerate(history_rows, start=1):
        status = status_labels.get(row["status"], row["status"])
        lines.append(
            f"<b>{index}. {html.escape(row['newspaper'])}</b> · {status}"
        )
        lines.append(f"└─ {html.escape(row['filename'])}")
        if row.get("published_date"):
            lines.append(f"   📅 {html.escape(row['published_date'])}")
        if row.get("error"):
            lines.append(f"   ⚠️ {html.escape(row['error'])}")
        if row.get("drive_url"):
            buttons.append(
                [{"text": f"ဖိုင်ဖွင့်ရန် · {row['filename']}", "url": row["drive_url"]}]
            )
    return "\n".join(lines), {"inline_keyboard": buttons} if buttons else None


def build_idle_notification(date_label):
    return (
        "\n".join(
            [
                "<b>ℹ️ ယနေ့အတွက် အသစ်တင်ရန် မရှိသေးပါ</b>",
                f"<i>📅 ရက်စွဲ — {html.escape(date_label)}</i>",
                "",
                "ယနေ့စာစောင်သည် Google Drive ထဲတွင် ရှိပြီးသားဖြစ်နိုင်ပါသည်၊ သို့မဟုတ် source website တွင် စာစောင်အသစ် မထွက်သေးပါ။",
            ]
        ),
        None,
    )
