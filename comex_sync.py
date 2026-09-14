from __future__ import annotations

import csv
import ftplib
import json
import os
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


# ============================================================
# KONFIGURACJA
# ============================================================

OUTPUT_FILE = Path("comex.csv")

NBP_URL = (
    "https://api.nbp.pl/api/exchangerates/"
    "rates/A/USD/?format=json"
)

# Feed obecnie ma ponad 300 produktow.
# Jezeli nagle dostaniemy np. 20 produktow,
# nie publikujemy pliku do Selly.
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


# ============================================================
# SECRETS / ENV
# ============================================================

def get_env(name: str) -> str:
    value = os.environ.get(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Brak wymaganej zmiennej / Secret: {name}"
        )

    return value


# ============================================================
# COMEX - POBIERANIE PRZEZ CURL
# ============================================================

def download_comex(url: str) -> bytes:
    """
    Pobiera feed COMEX przez curl.

    --compressed jest tutaj bardzo wazne:
    curl automatycznie obsluguje gzip / deflate / brotli,
    jezeli serwer zwroci XML w postaci skompresowanej.
    """

    print("Pobieram XML COMEX przez curl...")

    with tempfile.NamedTemporaryFile(
        suffix=".xml",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)

    try:
        command = [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--compressed",
            "--retry",
            "3",
            "--retry-delay",
            "5",
            "--retry-all-errors",
            "--connect-timeout",
            "30",
            "--max-time",
            "120",
            "--user-agent",
            (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/153.0 Safari/537.36"
            ),
            "--header",
            "Accept: application/xml,text/xml,*/*",
            "--output",
            str(temp_path),
            url,
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise RuntimeError(
                "Nie udalo sie pobrac XML COMEX przez curl. "
                f"curl exit code: {result.returncode}. "
                f"Blad: {result.stderr.strip()}"
            )

        if not temp_path.exists():
            raise RuntimeError(
                "curl nie utworzyl pliku COMEX."
            )

        data = temp_path.read_bytes()

        if not data:
            raise RuntimeError(
                "COMEX zwrocil pusty plik."
            )

        print(
            f"Pobrano XML COMEX: {len(data)} bajtow."
        )

        # Jezeli mimo --compressed dostalibysmy surowy gzip.
        if data.startswith(b"\x1f\x8b"):
            raise RuntimeError(
                "COMEX nadal zwrocil surowe dane GZIP. "
                "Plik nie zostanie przetworzony."
            )

        # Podstawowa kontrola, czy to rzeczywiscie XML produktowy.
        preview = data[:1000].lstrip()

        if (
            b"<products" not in preview
            and b"<?xml" not in preview
        ):
            raise RuntimeError(
                "COMEX nie zwrocil oczekiwanego XML. "
                "Mozliwa odpowiedz HTML / komunikat bledu."
            )

        if b"</products>" not in data[-1000:]:
            raise RuntimeError(
                "XML COMEX wyglada na niepelny / uciety. "
                "Brakuje znacznika </products>."
            )

        return data

    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


# ============================================================
# NBP
# ============================================================

def download_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Kompleksmedia-Comex-Sync/2.0",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=60,
        ) as response:
            data = response.read()

    except Exception as exc:
        raise RuntimeError(
            f"Nie udalo sie pobrac danych z NBP: {exc}"
        ) from exc

    try:
        return json.loads(
            data.decode("utf-8")
        )

    except Exception as exc:
        raise RuntimeError(
            "NBP zwrocil nieprawidlowy JSON."
        ) from exc


def get_usd_rate() -> tuple[Decimal, str, str]:
    payload = download_json(NBP_URL)

    rates = payload.get("rates")

    if (
        not isinstance(rates, list)
        or not rates
    ):
        raise RuntimeError(
            "NBP nie zwrocil kursu USD."
        )

    rate_data = rates[0]

    try:
        rate = Decimal(
            str(rate_data["mid"])
        )

    except (
        KeyError,
        InvalidOperation,
    ) as exc:
        raise RuntimeError(
            "Nieprawidlowy kurs USD w odpowiedzi NBP."
        ) from exc

    if rate <= Decimal("0"):
        raise RuntimeError(
            f"Nieprawidlowy kurs USD/PLN: {rate}"
        )

    effective_date = str(
        rate_data.get(
            "effectiveDate",
            "",
        )
    )

    table_number = str(
        rate_data.get(
            "no",
            "",
        )
    )

    return (
        rate,
        effective_date,
        table_number,
    )


# ============================================================
# XML
# ============================================================

def get_xml_value(
    product: ET.Element,
    field: str,
) -> str:

    element = product.find(field)

    if (
        element is None
        or element.text is None
    ):
        raise RuntimeError(
            f"Brak pola <{field}> "
            "w jednym z produktow."
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

    if root.tag != "products":
        raise RuntimeError(
            f"Nieoczekiwany element glowny XML: "
            f"<{root.tag}>"
        )

    products = root.findall("./product")

    product_count = len(products)

    print(
        f"XML COMEX zawiera {product_count} produktow."
    )

    if product_count < MIN_PRODUCTS:
        raise RuntimeError(
            f"XML zawiera tylko {product_count} produktow. "
            f"Minimum bezpieczenstwa to {MIN_PRODUCTS}. "
            "Plik FTP nie zostanie nadpisany."
        )

    product_ids: set[str] = set()
    rows: list[dict[str, str]] = []

    for index, product in enumerate(
        products,
        start=1,
    ):

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
            quantity_int = int(quantity)
            product_id_int = int(product_id)
            usd_price = Decimal(
                usd_price_text
            )

        except (
            ValueError,
            InvalidOperation,
        ) as exc:
            raise RuntimeError(
                f"Nieprawidlowe dane liczbowe "
                f"w rekordzie #{index}, "
                f"product_id={product_id}"
            ) from exc

        if quantity_int < 0:
            raise RuntimeError(
                f"Ujemny stan magazynowy: "
                f"product_id={product_id}"
            )

        if product_id_int <= 0:
            raise RuntimeError(
                f"Nieprawidlowy product_id: "
                f"{product_id}"
            )

        if usd_price < Decimal("0"):
            raise RuntimeError(
                f"Ujemna cena USD: "
                f"product_id={product_id}"
            )

        if product_id in product_ids:
            raise RuntimeError(
                f"Powtorzony product_id: "
                f"{product_id}"
            )

        product_ids.add(product_id)

        # ====================================================
        # PRZELICZENIE USD -> PLN
        # ====================================================

        pln_price = (
            usd_price * usd_rate
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        pln_price_text = (
            f"{pln_price:.2f}"
        )

        # Trzy identyczne ceny bazowe.
        # Narzuty beda robione po stronie Selly.
        rows.append(
            {
                "quantity":
                    quantity,

                "mpn":
                    mpn,

                "product_id":
                    product_id,

                "name":
                    name,

                "category_default_name":
                    category,

                "sale_price_tax_excl":
                    usd_price_text,

                "sale_price_tax_excl_pln1":
                    pln_price_text,

                "sale_price_tax_excl_pln2":
                    pln_price_text,

                "sale_price_tax_excl_pln3":
                    pln_price_text,
            }
        )

    # ========================================================
    # CSV
    # ========================================================

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
            lineterminator="\n",
        )

        writer.writeheader()
        writer.writerows(rows)

    if (
        not OUTPUT_FILE.exists()
        or OUTPUT_FILE.stat().st_size == 0
    ):
        raise RuntimeError(
            "Wygenerowany comex.csv jest pusty."
        )

    return len(rows)


# ============================================================
# FTP / FTPS
# ============================================================

def connect_ftp() -> ftplib.FTP:

    host = get_env("FTP_HOST")
    user = get_env("FTP_USER")
    password = get_env("FTP_PASSWORD")

    mode = os.environ.get(
        "FTP_MODE",
        "FTP",
    ).strip().upper()

    print(
        f"Laczenie z FTP w trybie {mode}..."
    )

    if mode == "FTPS":

        ftp = ftplib.FTP_TLS()

        ftp.connect(
            host=host,
            port=21,
            timeout=60,
        )

        ftp.login(
            user=user,
            passwd=password,
        )

        ftp.prot_p()

    elif mode == "FTP":

        ftp = ftplib.FTP()

        ftp.connect(
            host=host,
            port=21,
            timeout=60,
        )

        ftp.login(
            user=user,
            passwd=password,
        )

    else:
        raise RuntimeError(
            "FTP_MODE musi miec wartosc "
            "FTP albo FTPS."
        )

    ftp.set_pasv(True)

    print(
        "Polaczenie FTP zostalo nawiazane."
    )

    return ftp


def ftp_missing(
    exc: ftplib.error_perm,
) -> bool:
    return str(exc).startswith("550")


def delete_if_exists(
    ftp: ftplib.FTP,
    filename: str,
) -> None:

    try:
        ftp.delete(filename)

    except ftplib.error_perm as exc:
        if not ftp_missing(exc):
            raise


def ensure_ftp_directory(
    ftp: ftplib.FTP,
    path: str,
) -> None:
    """
    Przechodzi do katalogu FTP.
    Jezeli katalog nie istnieje, probuje go utworzyc.
    """

    path = path.strip()

    if not path or path == "/":
        return

    if path.startswith("/"):
        ftp.cwd("/")

    parts = [
        part
        for part in path.split("/")
        if part
    ]

    for part in parts:
        try:
            ftp.cwd(part)

        except ftplib.error_perm as exc:

            if not ftp_missing(exc):
                raise

            print(
                f"Tworze katalog FTP: {part}"
            )

            ftp.mkd(part)
            ftp.cwd(part)


# ============================================================
# UPLOAD
# ============================================================

def upload_ftp() -> None:

    ftp_dir = os.environ.get(
        "FTP_DIR",
        "",
    ).strip()

    live_file = "comex.csv"
    temp_file = "comex.upload.csv"
    backup_file = "comex.prev.csv"

    ftp = connect_ftp()

    try:
        if ftp_dir:
            print(
                f"Katalog docelowy FTP: {ftp_dir}"
            )

            ensure_ftp_directory(
                ftp,
                ftp_dir,
            )

        # Stary temp usuwamy.
        delete_if_exists(
            ftp,
            temp_file,
        )

        print(
            f"Wysylam {temp_file}..."
        )

        with OUTPUT_FILE.open(
            "rb"
        ) as file:

            ftp.storbinary(
                f"STOR {temp_file}",
                file,
            )

        print(
            "Plik tymczasowy wyslany."
        )

        # Poprzedni backup usuwamy.
        delete_if_exists(
            ftp,
            backup_file,
        )

        old_live_exists = False

        # comex.csv -> comex.prev.csv
        try:
            ftp.rename(
                live_file,
                backup_file,
            )

            old_live_exists = True

            print(
                "Poprzedni comex.csv zapisany "
                "jako comex.prev.csv."
            )

        except ftplib.error_perm as exc:

            if not ftp_missing(exc):
                raise

            print(
                "Pierwsza publikacja - "
                "brak poprzedniego comex.csv."
            )

        # comex.upload.csv -> comex.csv
        try:
            ftp.rename(
                temp_file,
                live_file,
            )

        except Exception:

            if old_live_exists:
                print(
                    "Blad podmiany. "
                    "Przywracam poprzedni comex.csv..."
                )

                try:
                    delete_if_exists(
                        ftp,
                        live_file,
                    )

                    ftp.rename(
                        backup_file,
                        live_file,
                    )

                    print(
                        "Poprzedni comex.csv przywrocony."
                    )

                except Exception as rollback_error:

                    print(
                        "UWAGA: nie udalo sie wykonac "
                        f"rollback FTP: {rollback_error}"
                    )

            raise

        print(
            "FTP: poprawnie opublikowano comex.csv."
        )

    finally:
        try:
            ftp.quit()

        except Exception:
            ftp.close()


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    print(
        "========================================"
    )
    print(
        "COMEX -> SELLY"
    )
    print(
        "========================================"
    )

    comex_url = get_env(
        "COMEX_XML_URL"
    )

    # 1. COMEX
    xml_data = download_comex(
        comex_url
    )

    # 2. NBP
    print(
        "Pobieram aktualny kurs USD/PLN z NBP..."
    )

    (
        usd_rate,
        rate_date,
        table_number,
    ) = get_usd_rate()

    print(
        f"Kurs NBP: 1 USD = "
        f"{usd_rate} PLN"
    )

    print(
        f"Data kursu: {rate_date}"
    )

    print(
        f"Tabela NBP: {table_number}"
    )

    # 3. XML -> CSV
    print(
        "Przetwarzam XML COMEX..."
    )

    count = create_csv(
        xml_data,
        usd_rate,
    )

    print(
        f"Wygenerowano {count} produktow."
    )

    print(
        f"Rozmiar comex.csv: "
        f"{OUTPUT_FILE.stat().st_size} bajtow."
    )

    # 4. FTP
    print(
        "Wysylam comex.csv na FTP..."
    )

    upload_ftp()

    print(
        "========================================"
    )

    print(
        "SYNCHRONIZACJA ZAKONCZONA POPRAWNIE"
    )

    print(
        "========================================"
    )


if __name__ == "__main__":
    main()
