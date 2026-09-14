from __future__ import annotations

import csv
import ftplib
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


OUTPUT_FILE = Path("comex.csv")

NBP_URL = "https://api.nbp.pl/api/exchangerates/rates/A/USD/?format=json"

MIN_PRODUCTS = 100

CSV_FIELDS = [
    "quantity",
    "mpn",
    "product_id",
    "name",
    "category_default_name",
    "sale_price_tax_excl",
    "sale_price_tax_excl_pln1",
    "sale_price_tax_excl_pln2",
    "sale_price_tax_excl_pln3",
]


def get_env(name: str) -> str:
    value = os.environ.get(name, "").strip()

    if not value:
        raise RuntimeError(f"Brak wymaganej zmiennej: {name}")

    return value


def download(url: str, attempts: int = 3) -> bytes:
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Kompleksmedia-Comex-Sync/1.0"
                },
            )

            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()

                if not data:
                    raise RuntimeError("Serwer zwrocil pusty plik")

                return data

        except Exception as exc:
            last_error = exc

            if attempt < attempts:
                print(
                    f"Proba {attempt}/{attempts} nieudana: {exc}"
                )
                print("Ponawiam za 5 sekund...")
                time.sleep(5)

    raise RuntimeError(
        f"Nie udalo sie pobrac danych: {last_error}"
    )


def get_usd_rate() -> tuple[Decimal, str, str]:
    data = download(NBP_URL)

    payload = json.loads(data.decode("utf-8"))

    rates = payload.get("rates")

    if not rates:
        raise RuntimeError("NBP nie zwrocil kursu USD")

    rate_data = rates[0]

    try:
        rate = Decimal(str(rate_data["mid"]))
    except (KeyError, InvalidOperation) as exc:
        raise RuntimeError("Nieprawidlowy kurs USD z NBP") from exc

    if rate <= 0:
        raise RuntimeError("Kurs USD musi byc wiekszy od zera")

    effective_date = str(
        rate_data.get("effectiveDate", "")
    )

    table_number = str(
        rate_data.get("no", "")
    )

    return rate, effective_date, table_number


def get_xml_value(
    product: ET.Element,
    field: str,
) -> str:

    element = product.find(field)

    if element is None or element.text is None:
        raise RuntimeError(
            f"Brak pola {field} w produkcie"
        )

    return element.text.strip()


def create_csv(
    xml_data: bytes,
    usd_rate: Decimal,
) -> int:

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        raise RuntimeError(
            f"Nieprawidlowy XML COMEX: {exc}"
        ) from exc

    products = root.findall("./product")

    if len(products) < MIN_PRODUCTS:
        raise RuntimeError(
            f"XML zawiera tylko {len(products)} produktow. "
            f"Plik nie zostanie wyslany na FTP."
        )

    rows = []
    product_ids = set()

    for product in products:

        quantity = get_xml_value(
            product,
            "quantity",
        )

        mpn = get_xml_value(
            product,
            "mpn",
        )

        product_id = get_xml_value(
            product,
            "product_id",
        )

        name = get_xml_value(
            product,
            "name",
        )

        category = get_xml_value(
            product,
            "category_default_name",
        )

        usd_price_text = get_xml_value(
            product,
            "sale_price_tax_excl",
        )

        try:
            int(quantity)
            int(product_id)
            usd_price = Decimal(usd_price_text)

        except (ValueError, InvalidOperation) as exc:
            raise RuntimeError(
                f"Nieprawidlowe dane produktu ID {product_id}"
            ) from exc

        if product_id in product_ids:
            raise RuntimeError(
                f"Powtorzony product_id: {product_id}"
            )

        product_ids.add(product_id)

        pln_price = (
            usd_price * usd_rate
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        pln_price_text = f"{pln_price:.2f}"

        rows.append(
            {
                "quantity": quantity,
                "mpn": mpn,
                "product_id": product_id,
                "name": name,
                "category_default_name": category,
                "sale_price_tax_excl": usd_price_text,
                "sale_price_tax_excl_pln1": pln_price_text,
                "sale_price_tax_excl_pln2": pln_price_text,
                "sale_price_tax_excl_pln3": pln_price_text,
            }
        )

    with OUTPUT_FILE.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=CSV_FIELDS,
            delimiter=";",
            quotechar='"',
            quoting=csv.QUOTE_MINIMAL,
        )

        writer.writeheader()
        writer.writerows(rows)

    return len(rows)


def connect_ftp() -> ftplib.FTP:

    host = get_env("FTP_HOST")
    user = get_env("FTP_USER")
    password = get_env("FTP_PASSWORD")

    mode = os.environ.get(
        "FTP_MODE",
        "FTP",
    ).strip().upper()

    if mode == "FTPS":

        ftp = ftplib.FTP_TLS()

        ftp.connect(
            host,
            21,
            timeout=60,
        )

        ftp.login(
            user,
            password,
        )

        ftp.prot_p()

    else:

        ftp = ftplib.FTP()

        ftp.connect(
            host,
            21,
            timeout=60,
        )

        ftp.login(
            user,
            password,
        )

    ftp.set_pasv(True)

    return ftp


def delete_if_exists(
    ftp: ftplib.FTP,
    filename: str,
):

    try:
        ftp.delete(filename)

    except ftplib.error_perm as exc:

        if not str(exc).startswith("550"):
            raise


def upload_ftp():

    ftp = connect_ftp()

    ftp_dir = os.environ.get(
        "FTP_DIR",
        "",
    ).strip()

    live_file = "comex.csv"
    temp_file = "comex.upload.csv"
    backup_file = "comex.prev.csv"

    try:

        if ftp_dir:
            ftp.cwd(ftp_dir)

        delete_if_exists(
            ftp,
            temp_file,
        )

        with OUTPUT_FILE.open("rb") as file:

            ftp.storbinary(
                f"STOR {temp_file}",
                file,
            )

        delete_if_exists(
            ftp,
            backup_file,
        )

        live_exists = False

        try:

            ftp.rename(
                live_file,
                backup_file,
            )

            live_exists = True

        except ftplib.error_perm as exc:

            if not str(exc).startswith("550"):
                raise

        try:

            ftp.rename(
                temp_file,
                live_file,
            )

        except Exception:

            if live_exists:

                try:

                    delete_if_exists(
                        ftp,
                        live_file,
                    )

                    ftp.rename(
                        backup_file,
                        live_file,
                    )

                except Exception as rollback_error:

                    print(
                        f"Blad rollback FTP: {rollback_error}",
                        file=sys.stderr,
                    )

            raise

        print(f"FTP: zapisano {live_file}")

    finally:

        try:
            ftp.quit()
        except Exception:
            ftp.close()


def main():

    comex_url = get_env(
        "COMEX_XML_URL"
    )

    print("Pobieram XML COMEX...")

    xml_data = download(
        comex_url
    )

    print("Pobieram kurs USD/PLN z NBP...")

    usd_rate, rate_date, table = get_usd_rate()

    print(
        f"Kurs NBP: 1 USD = {usd_rate} PLN"
    )

    print(
        f"Data kursu: {rate_date}"
    )

    print(
        f"Tabela NBP: {table}"
    )

    count = create_csv(
        xml_data,
        usd_rate,
    )

    print(
        f"Wygenerowano {count} produktow"
    )

    upload_ftp()

    print(
        "Synchronizacja COMEX zakonczona."
    )


if __name__ == "__main__":
    main()
