# Telegram `/history` Command Listener

`telegram_bot.py` သည် Telegram Bot API long polling ကိုအသုံးပြု၍ admin chat မှ `/history` command ကို လက်ခံပြီး SQLite download history ကို compact status-card အဖြစ် ပြန်ပို့ပေးသည်။

## လိုအပ်သော environment variables

```bash
export TELEGRAM_BOT_TOKEN="<bot-token>"
export TELEGRAM_ADMIN_CHAT_ID="<your-admin-chat-id>"
export DATABASE_PATH="download_history.sqlite3"
```

`TELEGRAM_ADMIN_CHAT_ID` နှင့် ကိုက်ညီသော chat တစ်ခုတည်းကိုသာ command အသုံးပြုခွင့်ပေးသည်။ အခြား chat များမှ command များကို တိတ်တဆိတ် လျစ်လျူရှုသည်။

## Run လုပ်ပုံ

```bash
python telegram_bot.py
```

Command listener သည် အမြဲအလုပ်လုပ်နေသော Linux server/cloud host တစ်ခုတွင် run နေရမည်။ GitHub Actions ၏ schedule job သည် အလုပ်ပြီးလျှင် ပိတ်သွားသောကြောင့် long-polling listener အဖြစ် မသုံးသင့်ပါ။

## ရနိုင်သော command များ

| Command | လုပ်ဆောင်ချက် |
|---|---|
| `/history` | နောက်ဆုံး download history ၁၀ ခုနှင့် status summary ပြသည် |
| `/history 20` | နောက်ဆုံး history ၂၀ ခု ပြသည်။ အများဆုံး ၂၀ ခုသာ ခွင့်ပြုထားသည် |
| `/help` | ရနိုင်သော command များကို ပြသည် |
| `/start` | `/help` နှင့် တူညီသော အကူအညီ message ပြသည် |

History card ထဲတွင် စုစုပေါင်း record အရေအတွက်၊ uploaded/skipped/failed အရေအတွက်၊ newspaper အမည်၊ filename၊ published date၊ error message နှင့် Drive link ခလုတ်များ ပါဝင်သည်။

## လုံခြုံရေးမှတ်ချက်

Bot token ကို source code ထဲ မရေးရပါ။ Environment variable သို့မဟုတ် secret manager ထဲတွင်သာ သိမ်းရမည်။ Admin chat ID မသတ်မှတ်ထားပါက listener သည် မစတင်ပါ။
