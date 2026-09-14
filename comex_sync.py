from __future__ import annotations

import csv
import ftplib
import json
import os
import re
import sys
import time
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

# Zabezpieczenie przed opublikowaniem pustego/uszkodzonego feedu.
# Aktualny feed COMEX zawiera ponad 300 produktów.
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
# ZMIENNE ŚRODOWISKOWE
# ============================================================

def get_env(name: str) -> str:
    value = os.environ.get(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Brak wymaganej zmiennej / Secret: {name}"
        )

    return value


# ============================================================
# POBIERANIE HTTP
# ============================================================

def download(
    url: str,
    attempts: int = 3,
) -> bytes:

    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):

        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent":
                        "Kompleksmedia-Comex-Sync/1.0",
                    "Accept":
                        "application/xml,text/xml,"
                        "application/json,*/*",
                },
            )

            with urllib.request.urlopen(
                request,
                timeout=60,
            ) as response:

                data = response.read()

                if not data:
                    raise RuntimeError(
                        "Serwer zwrocil pusty plik"
                    )

                return data

        except Exception as exc:
            last_error = exc

            if attempt < attempts:
                print(
                    f"Proba {attempt}/{attempts} "
                    f"nieudana: {exc}"
                )

                print(
                    "Ponawiam za 5 sekund..."
                )

                time.sleep(5)

    raise RuntimeError(
        f"Nie udalo sie pobrac danych: "
        f"{last_error}"
    )


# ============================================================
# KURS USD / PLN - NBP
# ============================================================

def get_usd_rate() -> tuple[Decimal, str, str]:

    data = download(NBP_URL)

    try:
        payload = json.loads(
            data.decode("utf-8")
        )

    except Exception as exc:
        raise RuntimeError(
            "Nieprawidlowa odpowiedz JSON z NBP"
        ) from exc

    rates = payload.get("rates")

    if (
        not isinstance(rates, list)
        or not rates
    ):
        raise RuntimeError(
            "NBP nie zwrocil kursu USD"
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
            "Nieprawidlowy kurs USD z NBP"
        ) from exc

    if rate <= Decimal("0"):
        raise RuntimeError(
            "Kurs USD musi byc wiekszy od zera"
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
# NAPRAWA XML COMEX
# ============================================================

def sanitize_xml(
    xml_data: bytes,
) -> bytes:
    """
    COMEX potrafi zwrocic XML zawierajacy znaki,
    ktore standardowy parser XML odrzuca.

    Funkcja:
    - usuwa niedozwolone znaki sterujace XML 1.0,
    - poprawia niezakodowane znaki & poza CDATA,
    - pozostawia zawartosc CDATA bez zmian.
    """

    text = xml_data.decode(
        "utf-8-sig",
        errors="replace",
    )

    original_length = len(text)

    # Niedozwolone znaki sterujace w XML 1.0
    text = re.sub(
        r"[\x00-\x08\x0B\x0C\x0E-\x1F]",
        "",
        text,
    )

    # Dzielimy XML tak, aby nie ingerowac w CDATA.
    parts = re.split(
        r"(<!\[CDATA\[.*?\]\]>)",
        text,
        flags=re.DOTALL,
    )

    for index in range(
        0,
        len(parts),
        2,
    ):
        # Poza CDATA znak & musi byc encja XML.
        # Zachowujemy tylko encje obslugiwane przez XML.
        parts[index] = re.sub(
            r"&(?!(?:amp|lt|gt|quot|apos);"
            r"|#\d+;"
            r"|#x[0-9A-Fa-f]+;)",
            "&amp;",
            parts[index],
        )

    cleaned = "".join(parts)

    if len(cleaned) != original_length:
        print(
            "XML COMEX wymagal oczyszczenia "
            "przed parsowaniem."
        )

    return cleaned.encode("utf-8")


# ============================================================
# ODCZYT PÓL XML
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
            f"w produkcie"
        )

    return element.text.strip()


# ============================================================
# XML -> CSV
# ============================================================

def create_csv(
    xml_data: bytes,
    usd_rate: Decimal,
) -> int:

    cleaned_xml = sanitize_xml(
        xml_data
    )

    try:
        root = ET.fromstring(
            cleaned_xml
        )

    except ET.ParseError as exc:
        raise RuntimeError(
            f"Nieprawidlowy XML COMEX "
            f"po oczyszczeniu: {exc}"
        ) from exc

    if root.tag != "products":
        raise RuntimeError(
            f"Nieoczekiwany glowny element XML: "
            f"<{root.tag}>"
        )

    products = root.findall(
        "./product"
    )

    product_count = len(products)

    print(
        f"XML COMEX zawiera "
        f"{product_count} produktow."
    )

    if product_count < MIN_PRODUCTS:
        raise RuntimeError(
            f"XML zawiera tylko "
            f"{product_count} produktow. "
            f"Minimum bezpieczenstwa: "
            f"{MIN_PRODUCTS}. "
            f"Plik nie zostanie wyslany na FTP."
        )

    rows: list[
        dict[str, str]
    ] = []

    product_ids: set[str] = set()

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
            quantity_int = int(
                quantity
            )

            product_id_int = int(
                product_id
            )

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
                f"Ujemny stan magazynowy "
                f"dla product_id={product_id}"
            )

        if product_id_int <= 0:
            raise RuntimeError(
                f"Nieprawidlowy product_id: "
                f"{product_id}"
            )

        if usd_price < Decimal("0"):
            raise RuntimeError(
                f"Ujemna cena USD "
                f"dla product_id={product_id}"
            )

        if product_id in product_ids:
            raise RuntimeError(
                f"Powtorzony product_id: "
                f"{product_id}"
            )

        product_ids.add(
            product_id
        )

        # Cena PLN bazowa
        pln_price = (
            usd_price
            * usd_rate
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        pln_price_text = (
            f"{pln_price:.2f}"
        )

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
            "Wygenerowany CSV jest pusty"
        )

    return len(rows)


# ============================================================
# FTP / FTPS
# ============================================================

def connect_ftp() -> ftplib.FTP:

    host = get_env(
        "FTP_HOST"
    )

    user = get_env(
        "FTP_USER"
    )

    password = get_env(
        "FTP_PASSWORD"
    )

    mode = os.environ.get(
        "FTP_MODE",
        "FTP",
    ).strip().upper()

    print(
        f"Laczenie z serwerem "
        f"w trybie {mode}..."
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

        # Szyfrowanie transmisji danych
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
            "FTP albo FTPS"
        )

    ftp.set_pasv(True)

    print(
        "Polaczenie FTP/FTPS nawiazane."
    )

    return ftp


def ftp_file_missing(
    exc: ftplib.error_perm,
) -> bool:

    return str(exc).startswith(
        "550"
    )


def delete_if_exists(
    ftp: ftplib.FTP,
    filename: str,
) -> None:

    try:
        ftp.delete(
            filename
        )

    except ftplib.error_perm as exc:

        if not ftp_file_missing(
            exc
        ):
            raise


# ============================================================
# WYSYŁKA NA FTP
# ============================================================

def upload_ftp() -> None:

    ftp = connect_ftp()

    ftp_dir = os.environ.get(
        "FTP_DIR",
        "",
    ).strip()

    live_file = (
        "comex.csv"
    )

    temp_file = (
        "comex.upload.csv"
    )

    backup_file = (
        "comex.prev.csv"
    )

    try:

        if ftp_dir:
            print(
                f"Przechodze do katalogu FTP: "
                f"{ftp_dir}"
            )

            ftp.cwd(
                ftp_dir
            )

        # Usuwamy ewentualny stary plik tymczasowy.
        delete_if_exists(
            ftp,
            temp_file,
        )

        print(
            f"Wysylam plik tymczasowy: "
            f"{temp_file}"
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

        # Usuwamy stara kopie zapasowa.
        delete_if_exists(
            ftp,
            backup_file,
        )

        live_exists = False

        # Obecny plik -> backup.
        try:
            ftp.rename(
                live_file,
                backup_file,
            )

            live_exists = True

            print(
                f"Poprzedni {live_file} "
                f"zapisano jako "
                f"{backup_file}"
            )

        except ftplib.error_perm as exc:

            if not ftp_file_missing(
                exc
            ):
                raise

            print(
                "Brak poprzedniego "
                "comex.csv - pierwsza publikacja."
            )

        # Plik tymczasowy -> właściwy.
        try:
            ftp.rename(
                temp_file,
                live_file,
            )

        except Exception:

            # Jesli podmiana sie nie uda,
            # probujemy przywrocic poprzedni plik.
            if live_exists:

                print(
                    "Blad publikacji. "
                    "Proba przywrocenia "
                    "poprzedniej wersji..."
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
                        "Poprzednia wersja "
                        "zostala przywrocona."
                    )

                except Exception as rollback_error:

                    print(
                        f"UWAGA: rollback FTP "
                        f"nie udal sie: "
                        f"{rollback_error}",
                        file=sys.stderr,
                    )

            raise

        print(
            f"FTP: poprawnie opublikowano "
            f"{live_file}"
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

    comex_url = get_env(
        "COMEX_XML_URL"
    )

    print(
        "======================================"
    )

    print(
        "COMEX -> SELLY"
    )

    print(
        "======================================"
    )

    print(
        "Pobieram XML COMEX..."
    )

    xml_data = download(
        comex_url
    )

    print(
        f"Pobrano XML: "
        f"{len(xml_data)} bajtow."
    )

    print(
        "Pobieram kurs USD/PLN z NBP..."
    )

    (
        usd_rate,
        rate_date,
        table,
    ) = get_usd_rate()

    print(
        f"Kurs NBP: "
        f"1 USD = {usd_rate} PLN"
    )

    print(
        f"Data kursu: "
        f"{rate_date}"
    )

    print(
        f"Tabela NBP: "
        f"{table}"
    )

    print(
        "Przetwarzam XML COMEX..."
    )

    count = create_csv(
        xml_data,
        usd_rate,
    )

    print(
        f"Wygenerowano "
        f"{count} produktow."
    )

    print(
        f"Plik lokalny: "
        f"{OUTPUT_FILE}"
    )

    print(
        f"Rozmiar CSV: "
        f"{OUTPUT_FILE.stat().st_size} bajtow."
    )

    print(
        "Wysylam plik na FTP..."
    )

    upload_ftp()

    print(
        "======================================"
    )

    print(
        "Synchronizacja COMEX "
        "zakonczona poprawnie."
    )

    print(
        "======================================"
    )


if __name__ == "__main__":
    main()
