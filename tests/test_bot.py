import os

os.environ["BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ["ADMIN_USER_ID"] = "1"
os.environ["SOURCE_CHANNEL"] = "@test_source"
os.environ["TARGET_CHANNEL"] = "@test_target"
os.environ["TELEGRAM_API_ID"] = "12345678"
os.environ["TELEGRAM_API_HASH"] = "test-hash"

import bot


def test_content_deduplication(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DB_PATH", tmp_path / "test.sqlite3")
    bot.init_db()

    content_hash = bot.claim_content("  Hello   Telegram ")
    assert content_hash
    assert bot.claim_content("hello telegram") is None

    bot.release_content(content_hash)
    assert bot.claim_content("hello telegram") == content_hash


def test_source_settings_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DB_PATH", tmp_path / "test.sqlite3")
    bot.init_db()
    source = bot.list_sources()[0]

    bot.update_source_settings(source["id"], keywords="games", prompt="Short", auto_publish=True)
    updated = bot.get_source(source["id"])
    assert updated["keywords"] == "games"
    assert updated["prompt"] == "Short"
    assert updated["auto_publish"] == 1


def test_analytics_by_source(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DB_PATH", tmp_path / "test.sqlite3")
    bot.init_db()
    source = bot.list_sources()[0]
    draft_id = bot.save_draft("text", None, None, "original", source["id"], 1)
    bot.set_status(draft_id, "published")
    rows = bot.analytics_by_source()
    assert rows[0]["status"] == "published"
    assert rows[0]["count"] == 1


def test_post_format_keeps_link_versions_and_tags():
    source = "Версии: 26.0 и выше\nТеги: #𝐦𝐨𝐝"
    result = bot.format_generated_post(
        "🌀 Мод\n\nПОДРОБНЕЕ О ДОПОЛНЕНИИ\nнажми, чтобы посмотреть\n\nВерсии: другое\nТеги: #mod",
        source,
        "https://t.me/example/10",
    )
    assert 'href="https://t.me/example/10"' in result
    assert "#𝐦𝐨𝐝" in result
    assert "<b>Версии:</b> 26.0 и выше" in result
