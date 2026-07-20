__author__ = 'Khiem Doan'
__github__ = 'https://github.com/khiemdoan'
__email__ = 'doankhiem.crazy@gmail.com'

from datetime import date, datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup
from cloudscraper import CloudScraper

from dtos import Power655PrizeRow, Power655Result, Power655ResultList

URL_DETAIL = 'https://vietlott.vn/vi/trung-thuong/ket-qua-trung-thuong/655'

DATA_DIR = Path('data/power655')

DRAW_ID_WIDTH = 5
MAIN_COUNT = 6
MAX_NUMBER = 55


def _parse_draw_id(text: str) -> str | None:
    t = text.strip().lstrip('#')
    if not t.isdigit():
        return None
    return t.zfill(DRAW_ID_WIDTH)


def _parse_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), '%d/%m/%Y').date()
    except ValueError:
        return None


def _parse_numbers_blob(text: str) -> tuple[list[str], str | None]:
    """Parse Vietlott's ``"192033454853|21"`` blob.

    The first ``MAIN_COUNT`` groups of 2 digits are the main numbers;
    the trailing group (after ``|``) is the bonus Power Number. Returns
    ``(numbers, bonus)`` where ``bonus`` is ``None`` if the separator
    or the bonus is missing.
    """
    text = text.strip().replace(' ', '')
    if not text:
        return [], None
    main_part, _, bonus_part = text.partition('|')
    main_digits: list[str] = [main_part[i:i + 2] for i in range(0, len(main_part), 2)
                              if main_part[i:i + 2].isdigit()]
    if len(main_digits) != MAIN_COUNT:
        return [], None
    bonus: str | None = None
    if bonus_part:
        # bonus is typically 1..2 digits (no leading zero).
        if bonus_part.isdigit() and 1 <= int(bonus_part) <= MAX_NUMBER:
            bonus = bonus_part.zfill(2)
        else:
            return [], None
    return main_digits, bonus


def _parse_prize_value(text: str) -> int:
    """Parse ``"35.272.733.700"`` → 35272733700."""
    return int(text.replace('.', '').replace(',', '').strip() or '0')


def _parse_prize_table(right: BeautifulSoup) -> tuple[int, int, list[Power655PrizeRow]]:
    """Parse the right-side block. Returns ``(jackpot1, jackpot2, prizes)``."""
    jackpot_values: list[int] = []
    for h3 in right.select('div.gt_jackpot h3'):
        jackpot_values.append(_parse_prize_value(h3.get_text(strip=True)))
    while len(jackpot_values) < 2:
        jackpot_values.append(0)

    prizes: list[Power655PrizeRow] = []
    table = right.find('table')
    if table is None:
        return jackpot_values[0], jackpot_values[1], prizes
    for tr in table.select('tbody tr'):
        cells = tr.find_all('td')
        if len(cells) != 4:
            continue
        prizes.append(Power655PrizeRow(
            prize=cells[0].get_text(strip=True),
            numbers=cells[1].get_text(' ', strip=True),
            winner_count=int(cells[2].get_text(strip=True).replace('.', '').replace(',', '') or '0'),
            prize_value=_parse_prize_value(cells[3].get_text(strip=True)),
        ))
    return jackpot_values[0], jackpot_values[1], prizes


def _parse_detail(html: str) -> Power655Result | None:
    """Parse the detail page HTML into a Power655Result, or ``None``."""
    soup = BeautifulSoup(html, 'lxml')
    title = soup.select_one('div.chitietketqua_title')
    if title is None:
        return None
    b_tags = title.find_all('b')
    if len(b_tags) < 2:
        return None

    draw_id = _parse_draw_id(b_tags[0].get_text(strip=True))
    parsed_date = _parse_date(b_tags[1].get_text(strip=True))
    if draw_id is None or parsed_date is None:
        return None

    blob_el = soup.select_one('div.day_so_ket_qua_v2')
    if blob_el is None:
        return None
    numbers, bonus = _parse_numbers_blob(blob_el.get_text(strip=True))
    if not numbers or bonus is None:
        return None

    right = soup.select_one('#divRightContent')
    if right is None:
        return None
    jackpot1, jackpot2, prizes = _parse_prize_table(right)

    return Power655Result(
        date=parsed_date,
        draw_id=draw_id,
        numbers=numbers,
        bonus=bonus,
        jackpot1_value=jackpot1,
        jackpot2_value=jackpot2,
        prizes=prizes,
    )


class Power655:
    def __init__(self) -> None:
        self._http = CloudScraper()
        self._data: dict[tuple[date, str], Power655Result] = {}
        self._raw_data: pd.DataFrame = pd.DataFrame()

    def load(self) -> None:
        path = DATA_DIR / 'power655.json'
        if not path.exists():
            return
        with path.open('r', encoding='utf-8') as f:
            data = Power655ResultList.model_validate_json(f.read())
        for d in data.root:
            self._data[(d.date, d.draw_id)] = d
        self.generate_dataframe()

    def fetch(self) -> list[Power655Result]:
        """Fetch the most recent draw (with full prize table).

        Returns the list of newly fetched results whose ``(date, draw_id)``
        was not already in the cache. Empty list if no new draw.
        """
        resp = self._http.get(URL_DETAIL)
        if resp.status_code != 200:
            return []
        result = _parse_detail(resp.text)
        if result is None:
            return []
        key = (result.date, result.draw_id)
        if key in self._data:
            return []
        self._data[key] = result
        self.generate_dataframe()
        return [result]

    def generate_dataframe(self) -> None:
        records = [d.model_dump() for d in self._data.values()]
        self._raw_data = pd.DataFrame(records)
        if not self._raw_data.empty:
            self._raw_data['date'] = pd.to_datetime(self._raw_data['date'])
            self._raw_data['draw_id'] = self._raw_data['draw_id'].astype('string')

    def dump(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        records = sorted(
            (d.model_dump() for d in self._data.values()),
            key=lambda r: (r['date'], int(r['draw_id'])),
        )
        result_list = Power655ResultList.model_validate([Power655Result(**r) for r in records])

        df = pd.DataFrame(records)
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])

        with open(DATA_DIR / 'power655.json', 'w', encoding='utf-8') as f:
            f.write(result_list.model_dump_json(indent=2))

        df.to_csv(DATA_DIR / 'power655.csv', index=False)
        df.to_parquet(DATA_DIR / 'power655.parquet', index=False)

    def get_raw_data(self) -> pd.DataFrame:
        return self._raw_data

    def get_last_draw_id(self) -> str | None:
        if not self._data:
            return None
        return max(d.draw_id for d in self._data.values())


if __name__ == '__main__':
    game = Power655()
    game.load()
    new = game.fetch()
    if new:
        for r in new:
            print(f'  {r.date} #{r.draw_id}: {" ".join(r.numbers)} | bonus={r.bonus}')
            for p in r.prizes:
                print(f'    {p.prize}: {p.numbers!r}  count={p.winner_count}  value={p.prize_value:,}')
    else:
        print('No new draws found.')
    game.dump()
    print(f'Total draws stored: {len(game._data)}')
    print(f'Last draw id: {game.get_last_draw_id()}')