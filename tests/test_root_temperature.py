"""내부 FM 호출의 샘플링 온도 전달 계약."""

from __future__ import annotations

import asyncio
import unittest

import app


class _Response:
    status_code = 200

    def json(self) -> dict:
        return {"choices": [{"message": {"content": "ok"}}]}


class _Client:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def post(self, _url: str, **kwargs) -> _Response:
        self.payloads.append(kwargs["json"])
        return _Response()


class TemperatureForwardingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_client = app._client
        self.original_temperature = getattr(app, "FM_TEMPERATURE", None)
        self.client = _Client()
        app._client = self.client

    def tearDown(self) -> None:
        app._client = self.original_client
        app.FM_TEMPERATURE = self.original_temperature

    def test_forwards_configured_temperature_to_the_fm(self) -> None:
        """A/B로 지정한 온도는 분류·검색·생성 FM 호출에 동일하게 실려야 한다."""
        app.FM_TEMPERATURE = 0.0

        asyncio.run(app.call_fm([{"role": "user", "content": "hello"}], 128))

        self.assertIn("temperature", self.client.payloads[0])
        self.assertEqual(0.0, self.client.payloads[0]["temperature"])

    def test_omits_temperature_when_the_experiment_is_disabled(self) -> None:
        """미지정 상태는 기존 서버 기본 샘플링 동작을 바꾸지 않는다."""
        app.FM_TEMPERATURE = None

        asyncio.run(app.call_fm([{"role": "user", "content": "hello"}], 128))

        self.assertNotIn("temperature", self.client.payloads[0])


if __name__ == "__main__":
    unittest.main()
