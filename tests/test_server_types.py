"""Provider function routing and parsed plan types."""

import json
from decimal import Decimal
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase

import httpx

from mapper.features import map_features
from mapper.prices import map_prices
from parser.client import BillManagerClient
from parser.models import ParsedPlan, ParsedPrice
from parser.plans import parse_order_default_addons, parse_order_setup_fee, parse_pricelist


class ServerTypeParsingTests(TestCase):
    def test_price_list_preserves_selected_type(self) -> None:
        raw = (Path(__file__).parents[1] / "responce_examples/spacecore_prices.json").read_bytes()
        plans = parse_pricelist(raw, server_type="dedicated")
        self.assertTrue(plans)
        self.assertTrue(all(plan.server_type == "dedicated" for plan in plans))

    def test_dedicated_requires_known_setup_fee(self) -> None:
        price = ParsedPrice(period=1, cost="100", currency="eur")
        self.assertEqual(map_prices([price], server_type="dedicated"), [])
        with_fee = price.model_copy(update={"setup_cost": price.cost / 2})
        self.assertEqual(map_prices([with_fee], server_type="dedicated")[0]["setup_fee"], "50")
        self.assertEqual(map_prices([price], server_type="auction")[0]["setup_fee"], "0")
        self.assertEqual(map_prices([with_fee], server_type="auction"), [])

    def test_dedicated_setup_fee_is_parsed_from_price(self) -> None:
        raw = {"doc": {"list": [{"$name": "pricelist", "elem": [{
            "id": {"$": "51"}, "title": {"$": "Dedicated"},
            "datacenter": {"id": {"$": "4"}, "value": {"$": "Belgrade"}},
            "prices": {"price": [{
                "period": {"$": "1"}, "cost": {"$": "100"},
                "currency": {"$": "€"}, "setup": {"$": "25"},
            }]},
        }]}]}}
        plans = parse_pricelist(json.dumps(raw).encode(), server_type="dedicated")
        self.assertEqual(map_prices(plans[0].prices, "dedicated")[0]["setup_fee"], "25")

    def test_setup_fee_from_real_spacecore_order_response(self) -> None:
        order = {"doc": {"list": [{"$name": "pricelist_summary", "elem": [
            {"label": {"$": "Базовая стоимость"}, "cost": {"price": {"cost": {"$": "208.60"}}}},
            {"label": {"$": "Установка (разово)"}, "cost": {"price": {"cost": {"$": "103.60"}}}},
        ]}]}}
        self.assertEqual(parse_order_setup_fee(json.dumps(order).encode()), Decimal("103.60"))

    def test_spacecore_auction_default_configuration(self) -> None:
        order = {"doc": {
            "addon_16660": {"$": "398"},
            "slist": [{"$name": "addon_16660", "val": [
                {"$key": "398", "$": "32 GB RAM / 2x 4096 GB ENT.HDD INIC (0.00 €)"},
            ]}],
        }}
        raw = json.dumps(order).encode()
        plan = ParsedPlan(
            external_id=16659, server_type="auction", name="Intel Core i7-7700 [4c/8t] (4.20GHz)",
            description_raw="", detail={}, datacenter_id=89, datacenter_name="Germany",
            prices=[ParsedPrice(period=1, cost=Decimal("93.38"), currency="eur")],
            raw_order_param=raw,
        )
        self.assertEqual(parse_order_default_addons(raw), {"addon_16660": "398"})
        features = map_features(plan)
        self.assertIsNotNone(features)
        self.assertEqual((features.cores, features.ram, features.disk), (4, Decimal("32"), Decimal("8192")))

    def test_yacolo_hardware_from_title_without_detail(self) -> None:
        plan = ParsedPlan(
            external_id=25082, server_type="dedicated",
            name="i3-14100 3.5 ГГц 4 ядра / 32 ГБ DDR5 / 2x 1000 ГБ NVMe M.2",
            description_raw="", detail={}, datacenter_id=124, datacenter_name="Moscow",
            prices=[ParsedPrice(period=1, cost=Decimal("15000"), currency="rub")],
        )
        features = map_features(plan)
        self.assertIsNotNone(features)
        self.assertEqual((features.cores, features.ram, features.disk), (4, Decimal("32"), Decimal("2000")))


class ServerTypeClientTests(IsolatedAsyncioTestCase):
    async def test_billmanager_function_is_selected_by_type(self) -> None:
        calls: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.params["func"])
            return httpx.Response(200, content=b'{}')

        client = BillManagerClient("https://example.test", "user", "password")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            await client.fetch_pricelist(server_type="dedicated")
            await client.fetch_os_list(123, 4, server_type="auction")
        finally:
            await client._client.aclose()
        self.assertEqual(calls, ["v2.dedic.order.pricelist", "v2.not-install.order.param"])
