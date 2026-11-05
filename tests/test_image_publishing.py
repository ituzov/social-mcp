import pytest
from pydantic import ValidationError

from social_mcp.threads.schemas import ImagePostInput


def test_signed_image_url_and_approved_caption_are_preserved_exactly():
    url = "https://CDN.example.com:443/photo%2Fcover.jpg?signature=a%2Bb%2Fc&size=1&size=2"
    caption = "  Exact caption\nSecond line  "
    post = ImagePostInput(image_url=url, text=caption, alt_text="Cover description")
    assert post.image_url == url
    assert post.text == caption
    assert post.alt_text == "Cover description"
    assert url not in repr(post)
    assert caption not in repr(post)


def test_image_input_allows_missing_caption_and_alt_text():
    post = ImagePostInput(image_url="https://cdn.example.com/photo.jpg")
    assert post.text is None
    assert post.alt_text is None


def test_valid_text_boundaries_are_accepted_without_truncation():
    post = ImagePostInput(
        image_url="http://cdn.example.com/photo.jpg",
        text="x" * 500,
        alt_text="y" * 1000,
    )
    assert post.text == "x" * 500
    assert post.alt_text == "y" * 1000


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a URL",
        "/Users/example/photo.jpg",
        "file:///Users/example/photo.jpg",
        "data:image/png;base64,abc",
        "ftp://cdn.example.com/photo.jpg",
        "https://",
        " https://cdn.example.com/photo.jpg",
        "https://cdn.example.com/photo.jpg ",
        "https://cdn.example.com/photo name.jpg",
        "https://cdn.example.com/ph\noto.jpg",
        None,
        True,
        123,
    ],
)
def test_invalid_image_url_is_rejected(url):
    with pytest.raises(ValidationError):
        ImagePostInput.model_validate({"image_url": url})


@pytest.mark.parametrize(
    "fields",
    [
        {"text": "x" * 501},
        {"alt_text": "x" * 1001},
        {"text": 123},
        {"alt_text": True},
    ],
)
def test_invalid_caption_and_accessibility_text_are_rejected(fields):
    with pytest.raises(ValidationError):
        ImagePostInput.model_validate(
            {"image_url": "https://cdn.example.com/photo.jpg", **fields}
        )


def test_invalid_image_url_is_hidden_in_validation_error():
    secret = "synthetic-private-image-url"
    with pytest.raises(ValidationError) as caught:
        ImagePostInput(image_url=secret)
    assert secret not in str(caught.value)
