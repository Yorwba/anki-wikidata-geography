#!/usr/bin/env python3

import argparse
from collections import Counter
from contextlib import nullcontext
import datetime
import hashlib
import http.client
import os
import subprocess
import urllib.request
import colorlog  # https://github.com/borntyping/python-colorlog
from os.path import exists

import tempfile
import genanki
import numpy as np
from PIL import Image
from wikidata.client import Client
from wikidata.datavalue import DatavalueError
from wikidata.entity import EntityId

handler = colorlog.StreamHandler()
handler.setFormatter(colorlog.ColoredFormatter('%(log_color)s[%(levelname)s] %(message)s'))
logger = colorlog.getLogger('anki-geo')
logger.addHandler(handler)

CLIENT = Client()
SUBDIVISIONS = CLIENT.get(EntityId('P150'))
LOCATOR_MAP_IMAGE = CLIENT.get(EntityId('P242'))
INCEPTION = CLIENT.get(EntityId('P571'))
START_TIME = CLIENT.get(EntityId('P580'))
DISSOLVED = CLIENT.get(EntityId('P576'))
END_TIME = CLIENT.get(EntityId('P582'))
CAPITAL = CLIENT.get(EntityId('P36'))


def try_get_time_property(entity, prop):
    """
    :param entity:
    :param prop:
    :return:
    :rtype: datetime.date | None
    """
    try:
        result = entity.get(prop)
        if isinstance(result, int):  # if property contains an int, assume it is year and return the 1st of January
            result = datetime.date(result, 1, 1)
        return result
    except DatavalueError:
        return None  # TODO: fix wikidata package to handle dates better


def try_get_string_property(entity, prop):
    """
    :param entity:
    :param prop:
    :return:
    :rtype: wikidata.entity.Entity | None
    """
    try:
        result = entity.get(prop)
        return result
    except DatavalueError:
        return None


def try_get_label_in(entity, language):
    if language is None:
        return str(entity.label)
    try:
        return entity.label[language]
    except KeyError:
        if '-' in language:
            fallback = language.split('-')[0]
        else:
            fallback = None
        logger.warning(f'No {language} translation for {entity.label}, trying {fallback or "default"}.')
        return try_get_label_in(entity, fallback)


def try_get_wikilink_in(entity, language):
    if language is None:
        return next(iter(entity.data['sitelinks'].values()))['url']
    try:
        sitename = language.replace('-', '_')+'wiki'
        return entity.data['sitelinks'][sitename]['url']
    except KeyError:
        if '-' in language:
            fallback = language.split('-')[0]
        elif language == 'en':
            fallback = None
        else:
            fallback = 'en'
        logger.warning(f'No {language} wiki for {entity.label}, trying {fallback or "default"}.')
        return try_get_wikilink_in(entity, fallback)


def get_subdivisions(entity, date=None):
    if date is None:
        date = datetime.date.today()

    subdivisions = entity.getlist(SUBDIVISIONS)
    for subdivision in subdivisions:
        inception = try_get_time_property(subdivision, INCEPTION)
        start_time = try_get_time_property(subdivision, START_TIME)
        dissolved = try_get_time_property(subdivision, DISSOLVED)
        end_time = try_get_time_property(subdivision, END_TIME)
        if ((inception and inception > date)
                or (start_time and start_time > date)
                or (dissolved and dissolved < date)
                or (end_time and end_time < date)):
            continue
        yield subdivision


def get_locator_map_url(entity):
    try:
        maps = entity.getlist(LOCATOR_MAP_IMAGE)
    except:
        maps = []
    urls = [i.image_url for i in maps]
    svg_urls = [url for url in urls if url.endswith('.svg')]
    if svg_urls:
        return svg_urls[0]
    en_label = entity.label['en']
    if urls:
        logger.debug(f'No SVG map for {en_label}: {" ".join(urls)}')
        return urls[0]
    logger.warning(f'Warning: No map for {en_label}')
    return None


def download_locator_map(url, image_folder, filename):
    if url.endswith('.svg'):
        origin_map = f'{image_folder}/{filename}.svg'
        raster_map = f'{image_folder}/{filename}.png'
    else:
        origin_map = f'{image_folder}/{filename}.' + url.split('.')[-1]
        raster_map = f'{origin_map}'
    while True:
        req = urllib.request.Request(url)
        req.headers['User-Agent'] = 'AnkiWikidataGeography/1.0 (https://github.com/Yorwba/anki-wikidata-geography)'
        req.headers['Range'] = 'bytes=0-'
        with urllib.request.urlopen(req) as map_file:
            try:
                map_data = map_file.read()
                break
            except http.client.IncompleteRead:
                print(f'Incomplete read of {url} Retrying...')
    with open(origin_map, 'wb') as f:
        f.write(map_data)
    if origin_map.endswith('.svg') and not exists(raster_map):
        subprocess.run(['resvg', origin_map, raster_map])
    return origin_map, raster_map


def create_background_map(raster_maps, region_label, image_folder):
    raster_maps = [Image.open(i) for i in raster_maps]
    common_size = Counter(i.size for i in raster_maps).most_common(1)[0][0]
    raster_maps = [i for i in raster_maps if i.size == common_size]
    if len(raster_maps) == 1:
        logger.warning("Can't infer background from only a single map.")
        return None
    stacked_maps = np.array([np.array(i.convert('RGBA')) for i in raster_maps])
    median = np.median(stacked_maps, axis=0)
    background = Image.fromarray(np.array(median, dtype=stacked_maps.dtype))
    filename = f'{image_folder}/{region_label}.png'
    background.save(filename)
    return filename


REGION_SUBDIVISION_MODEL = genanki.Model(
    1175192202,  # generated by random.randrange(1 << 30, 1 << 31)
    'Subdivison of a Region',
    fields=[
        {'name': 'Subdivision'},
        {'name': 'Region'},
        {'name': 'Capital'},
        {'name': 'SubdivisionMap'},
        {'name': 'RegionMap'},
        {'name': 'WikidataId'},
        {'name': 'Language'},
        {'name': 'WikipediaLink'},
    ],
    templates=[
        {
            'name': 'Name from Map',
            'qfmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="subdivision">?</div>
                    <hr>
                    <div class="value">{{SubdivisionMap}}</div>
                ''',
            'afmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="subdivision">{{Subdivision}}</div>
                    <hr>
                    <div class="value">{{SubdivisionMap}}</div>
                    <hr>
                    <iframe src="{{WikipediaLink}}"
                        style="height: 100vh; width:100%;" seamless="seamless"></iframe>
                    <a href="https://www.wikidata.org/wiki/{{WikidataId}}">
                        Data source: Wikidata
                    </a>
                ''',
        },
        {
            'name': 'Map from Name',
            'qfmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="subdivision">{{Subdivision}}</div>
                    <hr>
                    <div class="value">{{RegionMap}}</div>
                ''',
            'afmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="subdivision">{{Subdivision}}</div>
                    <hr>
                    <div class="value">{{SubdivisionMap}}</div>
                    <hr>
                    <iframe src="{{WikipediaLink}}"
                        style="height: 100vh; width:100%;" seamless="seamless"></iframe>
                    <a href="https://www.wikidata.org/wiki/{{WikidataId}}">
                        Data source: Wikidata
                    </a>
                ''',
        },
        {
            'name': 'Capital from Subdivision',
            'qfmt':
                '''
                {{#Capital}}
                    <div id="region">{{Region}}</div>
                    <div id="subdivision">{{Subdivision}}</div>
                    <hr>
                    <div id="type">Capital</div>
                    <div id="capital">?</div>
                {{/Capital}}
                ''',
            'afmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="subdivision">{{Subdivision}}</div>
                    <hr>
                    <div id="type">Capital</div>
                    <div id="capital">{{Capital}}</div>
                    <iframe src="{{WikipediaLink}}"
                        style="height: 100vh; width:100%;" seamless="seamless"></iframe>
                    <a href="https://www.wikidata.org/wiki/{{WikidataId}}">
                        Data source: Wikidata
                    </a>
                ''',
        },
        {
            'name': 'Subdivision from Capital',
            'qfmt':
                '''
                {{#Capital}}
                    <div id="region">{{Region}}</div>
                    <div id="subdivision">?</div>
                    <hr>
                    <div id="type">Capital</div>
                    <div id="capital">{{Capital}}</div>
                {{/Capital}}
                ''',
            'afmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="subdivision">{{Subdivision}}</div>
                    <hr>
                    <div id="type">Capital</div>
                    <div id="capital">{{Capital}}</div>
                    <iframe src="{{WikipediaLink}}"
                        style="height: 100vh; width:100%;" seamless="seamless"></iframe>
                    <a href="https://www.wikidata.org/wiki/{{WikidataId}}">
                        Data source: Wikidata
                    </a>
                ''',
        },
    ],
    css='''
        .card {
            font-size: 20px;
            text-align: center;
        }
        .value > img {
            max-width: 100%;
            height: auto;
        }
        #region {
            color: gray;
            font-size: 20px;
        }
        #subdivision {
            font-size: 24px;
        }
        #type {
            color: gray;
            font-size: 20px;
        }
        #capital {
            font-size: 24px;
        }
    ''',
)


class RegionSubdivisionNote(genanki.Note):
    @property
    def guid(self):
        """
        GUID for the note is calculated based on the wikidata ID of the corresponding page
        """
        return genanki.guid_for(*self.fields[5:7])


DECK_ID_BASE = 1290639408  # generated by random.randrange(1 << 30, 1 << 31)


def main(argv):
    parser = argparse.ArgumentParser(description='Generate Anki geography deck from Wikidata')
    parser.add_argument('region', help="Wikidata Q-item identifier")
    parser.add_argument('--language', default='en', help="Language of the generated deck")
    parser.add_argument('--image-folder', default=None, help="Folder to store images, new temporary folder by default")
    parser.add_argument('--log-level', default='INFO', choices=['FATAL', 'ERROR', 'WARN', 'INFO', 'DEBUG'])
    args = parser.parse_args(argv[1:])

    logger.setLevel(args.log_level)

    region = CLIENT.get(args.region, load=True)
    region_label = try_get_label_in(region, args.language)
    logger.info(f'Building deck for {region_label}')

    image_folder = args.image_folder
    if image_folder is not None:
        if not os.path.exists(image_folder):
            os.mkdir(image_folder)
        context_manager = nullcontext(image_folder)
    else:
        context_manager = tempfile.TemporaryDirectory()

    with context_manager as image_folder:
        logger.debug(f'Image folder {image_folder}')
        subdivision_maps = {}
        for subdivision in get_subdivisions(region):
            subdivision_label = try_get_label_in(subdivision, args.language)
            logger.debug(f'Making map for {subdivision.id}, {subdivision_label}')
            locator_map_url = get_locator_map_url(subdivision)
            if locator_map_url is None:
                continue
            subdivision_maps[subdivision] = download_locator_map(locator_map_url, image_folder, subdivision_label)

        background_map = create_background_map(
            [raster for origin, raster in subdivision_maps.values()],
            region_label,
            image_folder
        )

        region_hash = hashlib.sha512(region_label.encode('utf-8')).digest()
        region_hashsum = np.frombuffer(region_hash, dtype=np.int32).sum()
        possible_ids = range(1 << 30, 1 << 31)
        deck_id = possible_ids[(DECK_ID_BASE + region_hashsum) % len(possible_ids)]
        deck_name = f'Administrative Subdivisions of {region_label}'
        deck = genanki.Deck(deck_id, deck_name)

        media_files = [background_map]

        for subdivision, maps in subdivision_maps.items():
            subdivision_label = try_get_label_in(subdivision, args.language)
            smallest_map = min(maps, key=lambda path: os.stat(path).st_size)
            media_files.append(smallest_map)
            logger.debug(f'Building cards for {subdivision.id}, {subdivision_label}')
            capital = try_get_string_property(subdivision, CAPITAL)
            if capital is None:
                capital_label = ""
            else:
                capital_label = try_get_label_in(capital, args.language)
                logger.debug(f'Capital: {capital.id}, {capital_label}')
            deck.add_note(
                RegionSubdivisionNote(
                    model=REGION_SUBDIVISION_MODEL,
                    fields=[
                        subdivision_label,
                        region_label,
                        capital_label,
                        f'<img src="{os.path.basename(smallest_map)}">',
                        f'<img src="{os.path.basename(background_map)}">',
                        subdivision.id,
                        args.language,
                        try_get_wikilink_in(subdivision, args.language),
                    ]
                )
            )

        package = genanki.Package(deck, media_files)
        package.write_to_file(deck_name + '.apkg')
        logger.info(f'Wrote {len(subdivision_maps)} cards to "{deck_name}.apkg"')


if __name__ == '__main__':
    import sys

    main(sys.argv)
