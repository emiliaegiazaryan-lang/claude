#!/usr/bin/env python3
"""
Wildberries Sales Data Export Script
ИП Полынцев Я.

Выгружает из API Wildberries:
 - Данные о продажах (количество, сумма)
 - Динамику выручки (рост/падение выручки по периодам)
 - Динамику средней цены продажи
 - Рекламные затраты

Официальная документация: https://dev.wildberries.ru/en
"""

import os
import sys
import time
import logging
import json
from datetime import datetime, timedelta, date
from typing import Optional

import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
STATISTICS_TOKEN = os.getenv("WB_STATISTICS_TOKEN", "")   # Токен категории «Статистика»
ANALYTICS_TOKEN  = os.getenv("WB_ANALYTICS_TOKEN", "")    # Токен категории «Аналитика»
ADVERTISING_TOKEN = os.getenv("WB_ADVERTISING_TOKEN", "") # Токен категории «Продвижение»

STATISTICS_BASE_URL  = "https://statistics-api.wildberries.ru"
ANALYTICS_BASE_URL   = "https://analytics-api.wildberries.ru"
ADVERTISING_BASE_URL = "https://advert-api.wildberries.ru"

REQUEST_DELAY = 0.3   # сек между запросами
MAX_RETRIES   = 4


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _get(url: str, token: str, params: Optional[dict] = None) -> dict | list:
    """HTTP GET с повторными попытками и задержкой."""
    headers = {"Authorization": token}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=60)
            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("Rate limit, жду %ds...", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp.json()
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise
            log.warning("Ошибка запроса (%s), попытка %d/%d", exc, attempt, MAX_RETRIES)
            time.sleep(2 ** attempt)


def _post(url: str, token: str, body: dict | list) -> dict | list:
    """HTTP POST с повторными попытками и задержкой."""
    headers = {"Authorization": token, "Content-Type": "application/json"}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=60)
            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("Rate limit, жду %ds...", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp.json()
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise
            log.warning("Ошибка запроса (%s), попытка %d/%d", exc, attempt, MAX_RETRIES)
            time.sleep(2 ** attempt)


def fmt_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 1. Детализированный отчёт о реализации (Statistics API)
# ---------------------------------------------------------------------------

def fetch_sales_report(date_from: date, date_to: date) -> pd.DataFrame:
    """
    Загружает детализированный отчёт реализации через пагинацию по rrdid.
    Endpoint: GET /api/v5/supplier/reportDetailByPeriod
    """
    if not STATISTICS_TOKEN:
        raise ValueError("WB_STATISTICS_TOKEN не задан в .env")

    url = f"{STATISTICS_BASE_URL}/api/v5/supplier/reportDetailByPeriod"
    rows = []
    rrdid = 0
    limit = 100_000

    log.info("Загрузка отчёта реализации %s — %s...", date_from, date_to)
    while True:
        params = {
            "dateFrom": fmt_date(date_from),
            "dateTo":   fmt_date(date_to),
            "rrdid":    rrdid,
            "limit":    limit,
        }
        chunk = _get(url, STATISTICS_TOKEN, params)
        if not chunk:
            break
        rows.extend(chunk)
        rrdid = chunk[-1].get("rrd_id", 0)
        log.info("  Получено строк: %d (всего: %d)", len(chunk), len(rows))
        if len(chunk) < limit:
            break
        time.sleep(60)  # API: макс 1 запрос/мин для этого метода

    if not rows:
        log.warning("Отчёт реализации пуст.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    return df


# ---------------------------------------------------------------------------
# 2. NM-отчёт по товарам (Analytics API)
# ---------------------------------------------------------------------------

def fetch_nm_report(date_from: date, date_to: date, page: int = 1) -> pd.DataFrame:
    """
    Загружает отчёт по артикулам с воронкой продаж.
    Endpoint: POST /api/v2/nm-report/detail
    """
    if not ANALYTICS_TOKEN:
        raise ValueError("WB_ANALYTICS_TOKEN не задан в .env")

    url = f"{ANALYTICS_BASE_URL}/api/v2/nm-report/detail"
    all_rows = []
    current_page = 1

    log.info("Загрузка NM-отчёта %s — %s...", date_from, date_to)
    while True:
        body = {
            "brandNames": [],
            "objectIDs":  [],
            "tagIDs":     [],
            "nmIDs":      [],
            "timezone":   "Europe/Moscow",
            "period": {
                "begin": f"{fmt_date(date_from)} 00:00:00",
                "end":   f"{fmt_date(date_to)} 23:59:59",
            },
            "orderBy": {"field": "ordersSumRub", "mode": "desc"},
            "page": current_page,
        }
        resp = _post(url, ANALYTICS_TOKEN, body)
        data = resp.get("data", {})
        cards = data.get("cards", [])
        if not cards:
            break
        all_rows.extend(cards)
        log.info("  Страница %d: %d товаров", current_page, len(cards))
        if not data.get("isNextPage", False):
            break
        current_page += 1

    if not all_rows:
        log.warning("NM-отчёт пуст.")
        return pd.DataFrame()

    rows_flat = []
    for card in all_rows:
        base = {
            "nmID":         card.get("nmID"),
            "vendorCode":   card.get("vendorCode"),
            "brandName":    card.get("brandName"),
            "objectName":   card.get("objectName"),
            "tags":         ", ".join(str(t) for t in card.get("tags", [])),
        }
        stats = card.get("statistics", {}).get("selectedPeriod", {})
        base.update({
            "openCardCount":         stats.get("openCardCount", 0),
            "addToCartCount":        stats.get("addToCartCount", 0),
            "ordersCount":           stats.get("ordersCount", 0),
            "ordersSumRub":          stats.get("ordersSumRub", 0),
            "buyoutsCount":          stats.get("buyoutsCount", 0),
            "buyoutsSumRub":         stats.get("buyoutsSumRub", 0),
            "cancelCount":           stats.get("cancelCount", 0),
            "cancelSumRub":          stats.get("cancelSumRub", 0),
            "addToCartConversion":   stats.get("addToCartConversion", 0),
            "cartToOrderConversion": stats.get("cartToOrderConversion", 0),
            "buyoutPercent":         stats.get("buyoutPercent", 0),
        })
        rows_flat.append(base)

    return pd.DataFrame(rows_flat)


# ---------------------------------------------------------------------------
# 3. Рекламные затраты (Advertising API)
# ---------------------------------------------------------------------------

def fetch_advert_campaigns() -> list[int]:
    """Получает список ID всех рекламных кампаний."""
    if not ADVERTISING_TOKEN:
        raise ValueError("WB_ADVERTISING_TOKEN не задан в .env")

    url = f"{ADVERTISING_BASE_URL}/adv/v1/promotion/adverts"
    # Статусы: -1=удалена, 4=готова, 7=завершена, 8=отказано, 9=активна, 11=пауза
    statuses   = [-1, 4, 7, 9, 11]
    advert_ids = []

    for status in statuses:
        params = {"status": status, "limit": 1000, "offset": 0}
        try:
            resp = _get(url, ADVERTISING_TOKEN, params)
            if isinstance(resp, list):
                for item in resp:
                    advert_ids.append(item["advertId"])
        except Exception as exc:
            log.warning("Не удалось получить кампании со статусом %d: %s", status, exc)

    log.info("Найдено кампаний: %d", len(advert_ids))
    return advert_ids


def fetch_advert_stats(date_from: date, date_to: date) -> pd.DataFrame:
    """
    Загружает статистику рекламных затрат.
    Endpoint: POST /adv/v3/fullstats
    """
    if not ADVERTISING_TOKEN:
        raise ValueError("WB_ADVERTISING_TOKEN не задан в .env")

    advert_ids = fetch_advert_campaigns()
    if not advert_ids:
        log.warning("Нет рекламных кампаний.")
        return pd.DataFrame()

    url = f"{ADVERTISING_BASE_URL}/adv/v3/fullstats"

    # Формируем список дат за период
    dates = []
    cur = date_from
    while cur <= date_to:
        dates.append(fmt_date(cur))
        cur += timedelta(days=1)

    # API принимает до 100 кампаний за раз
    chunk_size = 100
    all_rows = []

    for i in range(0, len(advert_ids), chunk_size):
        chunk_ids = advert_ids[i:i + chunk_size]
        body = [{"id": aid, "dates": dates} for aid in chunk_ids]
        log.info("  Запрос статистики кампаний %d-%d...", i + 1, i + len(chunk_ids))
        try:
            resp = _post(url, ADVERTISING_TOKEN, body)
            if isinstance(resp, list):
                for item in resp:
                    advert_id = item.get("advertId")
                    for day in item.get("days", []):
                        for app in day.get("apps", []):
                            for nm in app.get("nm", []):
                                all_rows.append({
                                    "advertId":  advert_id,
                                    "date":      day.get("date"),
                                    "appType":   app.get("appType"),
                                    "nmId":      nm.get("nmId"),
                                    "views":     nm.get("views", 0),
                                    "clicks":    nm.get("clicks", 0),
                                    "ctr":       nm.get("ctr", 0),
                                    "cpc":       nm.get("cpc", 0),
                                    "sum":       nm.get("sum", 0),      # Затраты, руб.
                                    "orders":    nm.get("orders", 0),
                                    "sum_price": nm.get("sum_price", 0),
                                })
        except Exception as exc:
            log.warning("Ошибка запроса статистики: %s", exc)

    if not all_rows:
        log.warning("Нет рекламных данных за период.")
        return pd.DataFrame()

    return pd.DataFrame(all_rows)


# ---------------------------------------------------------------------------
# 4. Расчёт аналитики: выручка, средняя цена, динамика
# ---------------------------------------------------------------------------

def calc_revenue_dynamics(df_curr: pd.DataFrame, df_prev: pd.DataFrame) -> pd.DataFrame:
    """Считает динамику выручки и средней цены между двумя периодами."""

    def summarize(df: pd.DataFrame) -> dict:
        if df.empty:
            return {"revenue": 0, "orders": 0, "avg_price": 0}
        # Колонки из отчёта реализации
        if "retail_price_withdisc_rub" in df.columns and "quantity_paid" in df.columns:
            df2 = df[df["doc_type_name"] == "Продажа"].copy() if "doc_type_name" in df.columns else df.copy()
            revenue = df2["retail_price_withdisc_rub"].fillna(0).sum()
            orders  = df2["quantity_paid"].fillna(0).sum()
            avg_price = revenue / orders if orders > 0 else 0
        else:
            revenue   = 0
            orders    = 0
            avg_price = 0
        return {"revenue": revenue, "orders": orders, "avg_price": avg_price}

    curr = summarize(df_curr)
    prev = summarize(df_prev)

    def delta_pct(curr_val, prev_val):
        if prev_val == 0:
            return None
        return round((curr_val - prev_val) / prev_val * 100, 2)

    return pd.DataFrame([{
        "Показатель":                      "Выручка (руб.)",
        "Текущий период":                  round(curr["revenue"], 2),
        "Предыдущий период":               round(prev["revenue"], 2),
        "Изменение (руб.)":                round(curr["revenue"] - prev["revenue"], 2),
        "Изменение (%)":                   delta_pct(curr["revenue"], prev["revenue"]),
    }, {
        "Показатель":                      "Кол-во продаж (шт.)",
        "Текущий период":                  int(curr["orders"]),
        "Предыдущий период":               int(prev["orders"]),
        "Изменение (шт.)":                 int(curr["orders"] - prev["orders"]),
        "Изменение (%)":                   delta_pct(curr["orders"], prev["orders"]),
    }, {
        "Показатель":                      "Средняя цена продажи (руб.)",
        "Текущий период":                  round(curr["avg_price"], 2),
        "Предыдущий период":               round(prev["avg_price"], 2),
        "Изменение (руб.)":                round(curr["avg_price"] - prev["avg_price"], 2),
        "Изменение (%)":                   delta_pct(curr["avg_price"], prev["avg_price"]),
    }])


def calc_advert_summary(df_adv: pd.DataFrame) -> pd.DataFrame:
    """Сводка по рекламным затратам."""
    if df_adv.empty:
        return pd.DataFrame(columns=["Показатель", "Значение"])

    total_sum    = df_adv["sum"].sum()
    total_clicks = df_adv["clicks"].sum()
    total_views  = df_adv["views"].sum()
    total_orders = df_adv["orders"].sum()
    avg_cpc      = total_sum / total_clicks if total_clicks > 0 else 0
    drr          = total_sum / df_adv["sum_price"].sum() * 100 if df_adv["sum_price"].sum() > 0 else 0

    return pd.DataFrame([
        {"Показатель": "Рекламные затраты (руб.)",   "Значение": round(total_sum, 2)},
        {"Показатель": "Показы",                      "Значение": int(total_views)},
        {"Показатель": "Клики",                       "Значение": int(total_clicks)},
        {"Показатель": "Заказы из рекламы",           "Значение": int(total_orders)},
        {"Показатель": "Средняя цена клика (руб.)",   "Значение": round(avg_cpc, 2)},
        {"Показатель": "ДРР (%)",                     "Значение": round(drr, 2)},
    ])


# ---------------------------------------------------------------------------
# 5. Экспорт в Excel
# ---------------------------------------------------------------------------

def export_to_excel(
    df_sales_curr:  pd.DataFrame,
    df_sales_prev:  pd.DataFrame,
    df_nm_curr:     pd.DataFrame,
    df_nm_prev:     pd.DataFrame,
    df_adv:         pd.DataFrame,
    date_from:      date,
    date_to:        date,
    output_path:    str,
):
    log.info("Формирование Excel-файла: %s", output_path)

    df_dynamics = calc_revenue_dynamics(df_sales_curr, df_sales_prev)
    df_adv_sum  = calc_advert_summary(df_adv)

    # Сводка по NM-отчёту (средняя цена по товару)
    def nm_with_avg_price(df: pd.DataFrame, label: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()
        df = df.copy()
        df["avg_price"] = df.apply(
            lambda r: round(r["ordersSumRub"] / r["ordersCount"], 2)
            if r.get("ordersCount", 0) > 0 else 0,
            axis=1,
        )
        df.rename(columns={
            "nmID":         "Артикул WB",
            "vendorCode":   "Артикул продавца",
            "brandName":    "Бренд",
            "objectName":   "Категория",
            "ordersCount":  "Заказы (шт.)",
            "ordersSumRub": "Сумма заказов (руб.)",
            "buyoutsCount": "Выкупы (шт.)",
            "buyoutsSumRub":"Сумма выкупов (руб.)",
            "cancelCount":  "Отмены (шт.)",
            "cancelSumRub": "Сумма отмен (руб.)",
            "avg_price":    "Средняя цена (руб.)",
            "buyoutPercent":"Процент выкупа (%)",
        }, inplace=True)
        return df[[
            "Артикул WB", "Артикул продавца", "Бренд", "Категория",
            "Заказы (шт.)", "Сумма заказов (руб.)", "Средняя цена (руб.)",
            "Выкупы (шт.)", "Сумма выкупов (руб.)",
            "Отмены (шт.)", "Сумма отмен (руб.)", "Процент выкупа (%)",
        ]]

    df_nm_curr_out = nm_with_avg_price(df_nm_curr, "curr")
    df_nm_prev_out = nm_with_avg_price(df_nm_prev, "prev")

    # Рекламные затраты по дням
    df_adv_daily = pd.DataFrame()
    if not df_adv.empty:
        df_adv_daily = (
            df_adv.groupby("date")
            .agg(
                Показы=("views", "sum"),
                Клики=("clicks", "sum"),
                Затраты_руб=("sum", "sum"),
                Заказы=("orders", "sum"),
                Выручка_из_рекламы=("sum_price", "sum"),
            )
            .reset_index()
            .rename(columns={"date": "Дата"})
        )
        df_adv_daily["ДРР_%"] = (
            df_adv_daily["Затраты_руб"] / df_adv_daily["Выручка_из_рекламы"].replace(0, float("nan")) * 100
        ).round(2)

    # Детализация продаж (из отчёта реализации)
    df_sales_detail = pd.DataFrame()
    if not df_sales_curr.empty:
        keep = [
            "realizationreport_id", "date_from", "date_to", "nm_id",
            "subject_name", "brand_name", "sa_name",
            "retail_price", "retail_price_withdisc_rub", "quantity",
            "quantity_paid", "discount_percent", "supplier_promo",
            "ppvz_for_pay", "penalty", "additional_payment",
            "doc_type_name", "order_dt", "sale_dt",
        ]
        existing = [c for c in keep if c in df_sales_curr.columns]
        df_sales_detail = df_sales_curr[existing].copy()

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        # Лист 1: Динамика выручки и средней цены
        df_dynamics.to_excel(writer, sheet_name="Динамика", index=False)

        # Лист 2: Рекламные затраты (сводка)
        df_adv_sum.to_excel(writer, sheet_name="Реклама_сводка", index=False)

        # Лист 3: Рекламные затраты по дням
        if not df_adv_daily.empty:
            df_adv_daily.to_excel(writer, sheet_name="Реклама_по_дням", index=False)

        # Лист 4: Товары — текущий период
        if not df_nm_curr_out.empty:
            df_nm_curr_out.to_excel(writer, sheet_name="Товары_текущий", index=False)

        # Лист 5: Товары — предыдущий период
        if not df_nm_prev_out.empty:
            df_nm_prev_out.to_excel(writer, sheet_name="Товары_предыдущий", index=False)

        # Лист 6: Детализация продаж (текущий период)
        if not df_sales_detail.empty:
            df_sales_detail.to_excel(writer, sheet_name="Продажи_детализация", index=False)

        # Лист 7: Реклама по кампаниям
        if not df_adv.empty:
            (
                df_adv.groupby("advertId")
                .agg(
                    Показы=("views", "sum"),
                    Клики=("clicks", "sum"),
                    Затраты_руб=("sum", "sum"),
                    Заказы=("orders", "sum"),
                    Выручка=("sum_price", "sum"),
                )
                .reset_index()
                .rename(columns={"advertId": "ID кампании"})
                .assign(**{"ДРР_%": lambda d: (
                    d["Затраты_руб"] / d["Выручка"].replace(0, float("nan")) * 100
                ).round(2)})
                .to_excel(writer, sheet_name="Реклама_по_кампаниям", index=False)
            )

    log.info("Файл сохранён: %s", output_path)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Выгрузка данных ИП Полынцев Я. из Wildberries"
    )
    parser.add_argument(
        "--date-from", required=True,
        help="Начало текущего периода (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--date-to", required=True,
        help="Конец текущего периода (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--compare-days", type=int, default=None,
        help=(
            "Количество дней предыдущего периода для сравнения. "
            "По умолчанию — такой же длительности, как текущий период."
        ),
    )
    parser.add_argument(
        "--output", default=None,
        help="Путь к выходному .xlsx файлу (по умолчанию — auto)"
    )
    parser.add_argument(
        "--skip-advert", action="store_true",
        help="Пропустить выгрузку рекламных данных"
    )
    parser.add_argument(
        "--skip-nm-report", action="store_true",
        help="Пропустить NM-отчёт (Analytics API)"
    )
    args = parser.parse_args()

    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date()
    date_to   = datetime.strptime(args.date_to,   "%Y-%m-%d").date()

    if date_from > date_to:
        log.error("date-from должна быть раньше date-to")
        sys.exit(1)

    period_days = (date_to - date_from).days + 1
    compare_days = args.compare_days or period_days
    prev_date_to   = date_from - timedelta(days=1)
    prev_date_from = prev_date_to - timedelta(days=compare_days - 1)

    log.info("=== Wildberries Export ИП Полынцев Я. ===")
    log.info("Текущий период:   %s — %s", date_from, date_to)
    log.info("Предыдущий период: %s — %s", prev_date_from, prev_date_to)

    output_path = args.output or (
        f"wb_report_{fmt_date(date_from)}_{fmt_date(date_to)}.xlsx"
    )

    # --- Загрузка данных ---
    df_sales_curr = pd.DataFrame()
    df_sales_prev = pd.DataFrame()

    if STATISTICS_TOKEN:
        df_sales_curr = fetch_sales_report(date_from, date_to)
        df_sales_prev = fetch_sales_report(prev_date_from, prev_date_to)
    else:
        log.warning("WB_STATISTICS_TOKEN не задан — пропускаем отчёт реализации.")

    df_nm_curr = pd.DataFrame()
    df_nm_prev = pd.DataFrame()

    if not args.skip_nm_report:
        if ANALYTICS_TOKEN:
            df_nm_curr = fetch_nm_report(date_from, date_to)
            df_nm_prev = fetch_nm_report(prev_date_from, prev_date_to)
        else:
            log.warning("WB_ANALYTICS_TOKEN не задан — пропускаем NM-отчёт.")

    df_adv = pd.DataFrame()
    if not args.skip_advert:
        if ADVERTISING_TOKEN:
            df_adv = fetch_advert_stats(date_from, date_to)
        else:
            log.warning("WB_ADVERTISING_TOKEN не задан — пропускаем рекламные данные.")

    export_to_excel(
        df_sales_curr, df_sales_prev,
        df_nm_curr, df_nm_prev,
        df_adv,
        date_from, date_to,
        output_path,
    )

    log.info("=== Готово! Файл: %s ===", output_path)


if __name__ == "__main__":
    main()
