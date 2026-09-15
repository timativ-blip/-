"""Import the user-provided TIK/UIK PDF; requires pypdf (development only)."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from pypdf import PdfReader


def extract_catalog(path):
    reader = PdfReader(path)
    text = ' '.join(' '.join(page.extract_text() for page in reader.pages).split())
    parts = re.split(r'ТИК Территориальная избирательная комиссия ', text)
    precincts = []
    tiks = set()
    for section in parts[1:]:
        name, _, _ = section.partition(' УИК ')
        tik = 'ТИК ' + name
        if tik in tiks:
            raise ValueError(f'Duplicate TIK: {tik}')
        tiks.add(tik)
        numbers = re.findall(r'Участковая избирательная комиссия №\s*(\d+)', section)
        if not numbers:
            raise ValueError(f'Empty TIK: {tik}')
        for number in numbers:
            precincts.append({'id': f'mo-uik-{number}', 'label': f'УИК № {number}', 'tik': tik})
    if len(precincts) != text.count('Участковая избирательная комиссия'):
        raise ValueError('Some UIK entries were not parsed')
    if len({p['id'] for p in precincts}) != len(precincts):
        raise ValueError('Duplicate UIK numbers')
    if not precincts:
        raise ValueError('Empty catalog')
    return precincts, len(reader.pages), len(tiks)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('pdf', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    precincts, pages, tiks = extract_catalog(args.pdf)
    args.output.write_text(json.dumps(precincts, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{pages} pages; {tiks} TIK; {len(precincts)} UIK')
    print('Source SHA256:', hashlib.sha256(args.pdf.read_bytes()).hexdigest())
