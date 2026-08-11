from app.notifier import listing_image_urls
from app.pipeline import extract_image_urls_from_details


class Record:
    image_urls = [
        "https://80.img.avito.st/image/1/1.Y6NJfraAz0o_0A1FAzdW6VzezU77yctK-66uTvsdwIjy3ctQP9ANRf8.xBWZJKVMHf1_efrLzty9blvQpW_1rCnjXUR-SEtK5xw",
        "https://80.img.avito.st/image/1/1.Y6NJfra4z0p_1w1PAzdW6VzezUz3301CP9rNSPnXx0D_.1Un2OlxQhOlx9zxyQzOmFKP-Bo5tQjVafMRkv55sMfs",
        "https://www.avito.st/static/ims/alfa_desktop_common_400x540.png",
        "https://example.com/banner.png",
    ]


def test_pipeline_filters_ad_images_from_details():
    urls = extract_image_urls_from_details(
        {
            "image_urls": [
                "https://80.img.avito.st/image/1/1.Y6NJfraAz0o_0A1FAzdW6VzezU77yctK-66uTvsdwIjy3ctQP9ANRf8.xBWZJKVMHf1_efrLzty9blvQpW_1rCnjXUR-SEtK5xw",
                "https://80.img.avito.st/image/1/1.Y6NJfra4z0p_1w1PAzdW6VzezUz3301CP9rNSPnXx0D_.1Un2OlxQhOlx9zxyQzOmFKP-Bo5tQjVafMRkv55sMfs",
                "https://www.avito.st/static/ims/alfa_desktop_common_400x540.png",
            ]
        }
    )
    assert urls == [
        "https://80.img.avito.st/image/1/1.Y6NJfraAz0o_0A1FAzdW6VzezU77yctK-66uTvsdwIjy3ctQP9ANRf8.xBWZJKVMHf1_efrLzty9blvQpW_1rCnjXUR-SEtK5xw"
    ]


def test_notifier_filters_ad_images_before_telegram():
    assert listing_image_urls(Record()) == [
        "https://80.img.avito.st/image/1/1.Y6NJfraAz0o_0A1FAzdW6VzezU77yctK-66uTvsdwIjy3ctQP9ANRf8.xBWZJKVMHf1_efrLzty9blvQpW_1rCnjXUR-SEtK5xw"
    ]
