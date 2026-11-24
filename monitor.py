"""チケットぴあの公演ページを監視し、「発売中」になったらDiscordに通知する。

監視するURLは urls.txt に1行ずつ書く（空行と # で始まる行は無視）。

環境変数:
  DISCORD_WEBHOOK_URL        通知を送るDiscordチャンネルのウェブフックURL（必須）
  DURATION_MINUTES           監視する時間・分（指定すると確認回数を自動で計算する）
  LOOP_COUNT                 1回の実行での確認回数（DURATION_MINUTES がないときだけ使う。既定 1）
  INTERVAL_SECONDS           確認の間隔・秒（既定 60）
  URL_GAP_SECONDS            URLごとの待ち時間・秒（既定 2）
  ERROR_ALERT_THRESHOLD      取得に連続で何回失敗したらDiscordで警告するか（既定 5）
  END_NOTICE_REMAINING       残り何回になったら「まもなく終了」をDiscordで知らせるか（既定 5）

使い方:
  python monitor.py                   通常の監視
  python monitor.py --announce 180    「これより180分間、確認を行います」とDiscordに送る
  python monitor.py --test            Discordにテスト通知だけ送る
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DEFAULT_URL = "https://t.pia.jp/pia/ticketInformation.do?eventCd=2628218&rlsCd=004&lotRlsCd="
URLS_FILE = Path(__file__).parent / "urls.txt"
STATE_FILE = Path(__file__).parent / "state.json"

# ページに出てくる受付状況の言葉
STATUSES = (
    "発売中",
    "予定枚数終了",
    "販売停止",
    "取り扱いなし",
    "発売前",
    "販売終了",
    "受付中",
    "受付終了",
    "売り切れ",
)
NOTIFY_STATUS = "発売中"  # この状態に変わったときだけ通知する

JST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}

UNKNOWN_EVENT = "(演目名不明)"
PRICE_RE = re.compile(r"[\d,]+\s*円")
DATE_RE = re.compile(r"(\d{4})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\s*\(([^)]*)\)")
START_RE = re.compile(r"(\d{1,2}:\d{2})\s*開演")
# 「(2026/11/15(日))」「(東京都・2026/11/15(日))」のような、末尾の括弧書きの日付
DATE_TAIL_RE = re.compile(r"\s*[（(][^（(]*\d{4}\s*/\s*\d{1,2}\s*/\s*\d{1,2}.*$")
# 括弧に入っていない日付（「2026/11/15(日)」）
BARE_DATE_RE = re.compile(r"\d{4}\s*/\s*\d{1,2}\s*/\s*\d{1,2}(\s*[（(][^）)]*[）)])?")
# 「＜１１月２９日（日）公演＞」のような、全角の山括弧つきの日付
ANGLE_DATE_RE = re.compile(r"\s*[＜<][^＞>]*[0-9０-９]+\s*月[^＞>]*[＞>]")


# ---------------------------------------------------------------- 読み込み
def load_urls() -> list[str]:
    env = os.environ.get("MONITOR_URL")
    if env:
        return [env.strip()]
    if URLS_FILE.exists():
        urls = []
        for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line not in urls:
                urls.append(line)
        if urls:
            return urls
    return [DEFAULT_URL]


def fetch(url: str) -> requests.Response:
    res = requests.get(url, headers=HEADERS, timeout=20)
    res.raise_for_status()
    # 文字コードの指定がないページで、文字化けしないようにする
    if not res.encoding or res.encoding.lower() == "iso-8859-1":
        res.encoding = res.apparent_encoding
    return res


# ---------------------------------------------------------------- ページの解析
def clean_event_name(raw: str) -> str:
    """ページのタイトルから、日付などを除いた演目名だけを取り出す。
    例: 舞台「〇〇」／大阪 | 当日引換券 - チケットぴあ → 舞台「〇〇」／大阪
        ＹＯＡＳＯＢＩ | 一般発売．＜１１月２９日（日）公演＞ → ＹＯＡＳＯＢＩ"""
    name = raw.strip()
    name = re.sub(r"^\s*チケットぴあ\s*[-－|｜]\s*", "", name)
    name = re.sub(r"\s*[-－|｜]\s*チケットぴあ.*$", "", name)
    name = re.split(r"\s+[|｜]\s+", name)[0]  # 「演目名 | 受付の名前」の、前半だけ
    name = DATE_TAIL_RE.sub("", name)
    name = BARE_DATE_RE.sub("", name)
    name = ANGLE_DATE_RE.sub("", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    name = re.sub(r"(^\s*[-－]\s+|\s+[-－]\s*$)", "", name).strip()
    return name or raw.strip()


def parse_event_name(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return clean_event_name(soup.title.string)
    return UNKNOWN_EVENT


def find_performance(lines: list[str], i: int) -> str | None:
    """lines[i] が公演の見出し（日付の行）なら「11/28(土) 12:00」の形で返す。
    開演時刻は、同じ行か、直後の2行から探す（例: 12:00 開演 ( 11:00 開場 )）。"""
    m = DATE_RE.match(lines[i])
    if not m:
        return None
    label = f"{int(m.group(2))}/{int(m.group(3))}({m.group(4)})"
    start = START_RE.search(" ".join(lines[i : i + 3]))
    if start:
        return f"{label} {start.group(1)}"
    # 時刻が読めないときは、日付だけの行（他の文が付いていない行）に限って採用する
    return label if DATE_RE.fullmatch(lines[i]) else None


def find_status(lines: list[str], i: int) -> str | None:
    """価格の行（lines[i]）に対応する受付状況を探す。
    同じ行の中、または直後の数行の先頭にある言葉を使う。"""
    for token in lines[i].split():
        if token in STATUSES:
            return token
    for ln in lines[i + 1 : i + 4]:
        parts = ln.split()
        if parts and parts[0] in STATUSES:
            return parts[0]
        if PRICE_RE.search(ln):
            break  # 次の券種に入ったので、探すのをやめる
    return None


def parse_ticket_details(html: str) -> dict[str, tuple[str, str | None, str]]:
    """キー → (受付状況, 公演日時, 券種名) を取り出す。

    価格（〇〇円）が書かれた行を、券種の行とみなし、同じ行か直後にある言葉を受付状況とする。
    公演日時は、その券種より前で一番近い公演の見出し（日付の行）から取る。
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()  # 画面に出ない部分の文字は、読まない
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]

    details: dict[str, tuple[str, str | None, str]] = {}
    performance: str | None = None
    for i, line in enumerate(lines):
        found = find_performance(lines, i)
        if found:
            performance = found
            continue
        if not PRICE_RE.search(line):
            continue
        status = find_status(lines, i)
        if not status:
            continue
        name = " ".join(t for t in line.split() if t not in STATUSES)
        if not re.search(r"[^\d,\s円]", name) and i > 0:  # 価格しかない行は、前の行を券種名にする
            name = f"{lines[i - 1]} {name}".strip()
        base = f"{performance or '-'}｜{name}"
        key, n = base, 2
        while key in details:  # 同じ名前が並んだときの衝突回避
            key, n = f"{base} #{n}", n + 1
        details[key] = (status, performance, name)
    return details


def format_restocked(
    restocked: list[str], details: dict[str, tuple[str, str | None, str]]
) -> str:
    """販売中になった券種を、公演日時ごとにまとめた文にする。
    例:
      11/28(土) 12:00
      ・当日引換券 12,800円"""
    groups: dict[str | None, list[str]] = {}
    for key in restocked:
        _, performance, name = details[key]
        groups.setdefault(performance, []).append(name)
    blocks = []
    for performance, names in groups.items():
        lines = [performance] if performance else []
        lines += [f"・{n}" for n in names]
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


# ---------------------------------------------------------------- Discord・状態
def send_discord(text: str) -> None:
    """DiscordのウェブフックURLにメッセージを送る（すべての通知で共通）。"""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK_URL が設定されていません")
    res = requests.post(webhook, json={"content": text}, timeout=15)
    res.raise_for_status()


def notify_safely(text: str) -> None:
    """Discordへの送信に失敗しても、監視は止めない。"""
    try:
        send_discord(text)
        print("通知しました")
    except Exception as e:
        print(f"通知に失敗しました: {e}", flush=True)


def load_state() -> dict[str, str]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict[str, str]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def state_key(url: str, key: str) -> str:
    return f"{url}\t{key}"


# ---------------------------------------------------------------- 監視
def check_once(url: str) -> bool:
    """1つのURLを1回確認する。取得に成功したら True。"""
    res = fetch(url)
    details = parse_ticket_details(res.text)
    if not details:
        print(f"受付状況を取得できませんでした（ページ構造の変更かアクセス制限の可能性）: {url}")
        return False

    event = parse_event_name(res.text)
    state = load_state()
    restocked = [
        key
        for key, (status, _, _) in details.items()
        if status == NOTIFY_STATUS and state.get(state_key(url, key)) != NOTIFY_STATUS
    ]

    print(f"[{event}]")
    for status, performance, name in details.values():
        print(f"  {status}: {name}  ({performance or '公演日時 不明'})")

    if restocked:
        send_discord(
            f"🎫 チケットが「{NOTIFY_STATUS}」になりました！\n"
            f"{event}\n{format_restocked(restocked, details)}\n\n{url}"
        )
        print("Discordに通知しました")

    # 通知に成功した場合のみ状態を保存（失敗時は次回また通知を試みる）
    prefix = f"{url}\t"
    state = {k: v for k, v in state.items() if not k.startswith(prefix)}
    for key, (status, _, _) in details.items():
        state[state_key(url, key)] = status
    save_state(state)
    return True


def format_interval(seconds: int) -> str:
    """確認の間隔を読みやすくする（例: 60 → 1分おき、50 → 50秒おき）。"""
    if seconds % 60 == 0:
        return f"{seconds // 60}分おき"
    return f"{seconds}秒おき"


def build_announcement(
    minutes: int,
    now: datetime | None = None,
    interval: int = 60,
    events: list[str] | None = None,
) -> str:
    now = now or datetime.now(JST)
    end = now + timedelta(minutes=minutes)
    day = "翌" if end.date() != now.date() else ""
    lines = [f"📢 これより{minutes}分間、チケット販売状況の確認を行います。"]
    lines += [f"・{name}" for name in (events or [])]
    lines.append(f"（{format_interval(interval)}に確認／{day}{end:%H:%M}ごろ終了予定）")
    return "\n".join(lines)


def fetch_event_names(urls: list[str]) -> list[str]:
    """各URLのページから演目名を取り出す。取得できなかったページは飛ばす。"""
    gap = int(os.environ.get("URL_GAP_SECONDS", "2"))
    names: list[str] = []
    for i, url in enumerate(urls):
        if i > 0:
            time.sleep(gap)
        try:
            name = parse_event_name(fetch(url).text)
        except Exception as e:
            print(f"演目名を取得できませんでした: {url}: {e}", flush=True)
            continue
        if name != UNKNOWN_EVENT and name not in names:
            names.append(name)
    return names


def main() -> int:
    if "--announce" in sys.argv:
        minutes = int(sys.argv[sys.argv.index("--announce") + 1])
        interval = int(os.environ.get("INTERVAL_SECONDS", "60"))
        events = fetch_event_names(load_urls())
        send_discord(build_announcement(minutes, interval=interval, events=events))
        print("開始のお知らせをDiscordに送信しました")
        return 0

    if "--test" in sys.argv:
        send_discord("【テスト】在庫通知の設定は完了しています。")
        print("テスト通知を送信しました")
        return 0

    urls = load_urls()
    interval = int(os.environ.get("INTERVAL_SECONDS", "60"))
    url_gap = int(os.environ.get("URL_GAP_SECONDS", "2"))
    if os.environ.get("DURATION_MINUTES"):
        # 監視する時間（分）から、確認の回数を求める。例: 300分・60秒おき → 300回
        loop_count = max(
            1, int(float(os.environ["DURATION_MINUTES"]) * 60 // max(interval, 1))
        )
    else:
        loop_count = max(1, int(os.environ.get("LOOP_COUNT", "1")))

    print(f"監視URL: {len(urls)}件 / 1回の実行で{loop_count}回確認")
    end_notice = max(1, int(os.environ.get("END_NOTICE_REMAINING", "5")))
    alert_threshold = max(1, int(os.environ.get("ERROR_ALERT_THRESHOLD", "5")))
    fail_streak: dict[str, int] = {}  # URLごとの「連続で取得に失敗した回数」
    alerted: dict[str, bool] = {}  # 警告を送信済みか

    ok_count = 0
    start = time.monotonic()
    for i in range(loop_count):
        if i > 0:
            # 通信にかかった時間を差し引いて、interval秒ごとの間隔を保つ（ずれを溜めない）
            wait = start + i * interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        if loop_count > end_notice and i == loop_count - end_notice:
            notify_safely(
                f"⏳ まもなく監視を終了します（あと{end_notice}回の確認で終了）。\n"
                "続けて監視する場合は、もう一度「Run workflow」を押してください。"
            )
        print(f"--- {i + 1}/{loop_count} 回目 ---", flush=True)
        for j, url in enumerate(urls):
            if j > 0:
                time.sleep(url_gap)  # 同時に大量アクセスしない
            try:
                ok = check_once(url)
            except Exception as e:  # 1つのURLの失敗で全体を止めない
                print(f"エラー: {url}: {e}", flush=True)
                ok = False

            if ok:
                ok_count += 1
                if alerted.get(url):
                    notify_safely(f"✅ 受付状況を取得できるようになりました\n{url}")
                    alerted[url] = False
                fail_streak[url] = 0
            else:
                fail_streak[url] = fail_streak.get(url, 0) + 1
                if fail_streak[url] >= alert_threshold and not alerted.get(url):
                    notify_safely(
                        f"⚠️ 受付状況を{fail_streak[url]}回続けて取得できていません\n"
                        f"アクセス制限やページ変更の可能性があります。\n{url}"
                    )
                    alerted[url] = True  # 復旧するまで、同じ警告は繰り返さない

    # 1回も成功しなかった場合だけ、失敗として終了する
    return 0 if ok_count else 1


if __name__ == "__main__":
    sys.exit(main())
