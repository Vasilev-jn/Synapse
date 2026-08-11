from app.notifier import telegram_caption_text, telegram_message_id


def test_telegram_message_id_reads_media_group_result() -> None:
    result = {
        "ok": True,
        "batches": [
            {
                "ok": True,
                "response": {
                    "ok": True,
                    "result": [
                        {"message_id": 111},
                        {"message_id": 112},
                    ],
                },
            }
        ],
    }

    assert telegram_message_id(result) == 111


def test_telegram_caption_text_keeps_caption_under_limit() -> None:
    text = "x" * 1200 + '\n\n<a href="https://www.avito.ru/item">open</a>'

    caption = telegram_caption_text(text)

    assert len(caption) <= 1000
    assert '<a href="https://www.avito.ru/item">open</a>' in caption
