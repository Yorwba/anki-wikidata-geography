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
import random
import requests
import json

import tempfile
import genanki
import numpy as np
from PIL import Image
from wikidata.client import Client
from wikidata.datavalue import DatavalueError
from wikidata.entity import EntityId


from make_map import make_map

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
COORDINATES = CLIENT.get(EntityId('P625'))
ISO2 = CLIENT.get(EntityId('P297'))
SUPERDIVISIONS = CLIENT.get(EntityId('P131'))


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


def get_top_cities(country_qcode):
    # this is pretty much chatgpt's work.
    # it's a little funny for some stuff (e.g. Honolulu county #10 in US),
    # but mostly seems to work?

    # Ensure 'country_qcode' is a string formatted as a Wikidata entity ID, e.g., 'Q30' for the United States.
    assert isinstance(country_qcode, str), "Country code must be a string representing a Wikidata Q-code."

    # Construct the SPARQL query
    query = """
    SELECT ?city ?cityLabel (MAX(?population) as ?maxPopulation) WHERE {
      ?city wdt:P31/wdt:P279* wd:Q515; # instance of (or subclass of) city
            wdt:P17 wd:%s; # located in the administrative territorial entity of the specified country
            wdt:P1082 ?population. # with population number
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
    }
    GROUP BY ?city ?cityLabel
    ORDER BY DESC(?maxPopulation)
    LIMIT 10
    """ % country_qcode

    # URL to Wikidata Query Service
    url = "https://query.wikidata.org/sparql"

    # Send the query
    response = requests.get(url, params={'query': query, 'format': 'json'})
    data = response.json()

    # Process the results
    cities = []
    for rank, item in enumerate(data['results']['bindings']):
        city_name = item['cityLabel']['value']
        population = int(item['maxPopulation']['value'])
        city_id = item['city']['value'].split('/')[-1]  # Extract the ID from the IRI
        cities.append({
            'city_name': city_name, 
            'population': population, 
            'city_id': city_id,
            'rank': rank + 1,
        })

    return cities


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


def get_current_superdivision(entity, date=None):
    if date is None:
        date = datetime.date.today()
    
    superdivisions = entity.getlist(SUPERDIVISIONS)

    # TODO make this smarter. I think we need to access the statements/qualifiers
    # of the non-super one directly, instead of loading the super identity.
    # for now, i think [0] returns most recent superdivision (though maybe not good for multiples).

    if superdivisions:
        return superdivisions[0]
    return None


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


def make_legible_number(num):
    # little chatgpt util
    if num >= 1000000:  # For millions
        return f"{num/1000000:.1f}M"
    elif num >= 1000:  # For thousands
        return f"{num/1000:.1f}k"
    else:  # For numbers less than 1000
        return str(num)


REGION_CITY_MODEL = genanki.Model(
    2056101586,  # generated by random.randrange(1 << 30, 1 << 31)
    'City in a Region',
    fields=[
        {'name': 'City'},
        {'name': 'Region'},
        {'name': 'Superdivision'},
        {'name': 'Population'},
        {'name': 'PopulationReadable'},
        {'name': 'PopulationRank'},
        {'name': 'CityMap'},
        {'name': 'RegionMap'},
        {'name': 'WikidataId'},
        {'name': 'Language'},
        {'name': 'WikipediaLink'},
        {'name': 'Latitude'},
        {'name': 'Longitude'},
        # TODO I remain a little unclear about whether it's ok if we add fields
        # not to the back here. i saw some weird behavior, but it's possible it went
        # away after restart.
    ],
    templates=[
        {
            'name': 'Name from Map',
            'qfmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="city">?</div>
                    <hr>
                    <div class="value">{{CityMap}}</div>
                ''',
            'afmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="city">{{City}}</div>
                    <hr>
                    <div class="value">{{CityMap}}</div>
                    <hr>
                    <iframe src="{{WikipediaLink}}"
                        style="height: 100vh; width:100%;" seamless="seamless"></iframe>
                    <a href="https://www.wikidata.org/wiki/{{WikidataId}}">
                        Data source: Wikidata
                    </a>
                ''',
        },
        {
            'name': 'Map from Name (with canvas)',
            'qfmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="city">{{City}}</div>
                    <hr>
                    <div class="value">{{RegionMap}}</div>
                ''',
            'afmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="city">{{City}}</div>
                    <hr>
                    <div class="value">{{CityMap}}</div>
                    <hr>
                    <iframe src="{{WikipediaLink}}"
                        style="height: 100vh; width:100%;" seamless="seamless"></iframe>
                    <a href="https://www.wikidata.org/wiki/{{WikidataId}}">
                        Data source: Wikidata
                    </a>
                ''',
        },
        {
            'name': 'Map from Name (without canvas)',
            'qfmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="city">{{City}}</div>
                    <hr>
                    <p class="prompt">Imagine location...</p>
                ''',
            'afmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="city">{{City}}</div>
                    <hr>
                    <div class="value">{{CityMap}}</div>
                    <hr>
                    <iframe src="{{WikipediaLink}}"
                        style="height: 100vh; width:100%;" seamless="seamless"></iframe>
                    <a href="https://www.wikidata.org/wiki/{{WikidataId}}">
                        Data source: Wikidata
                    </a>
                ''',
        },
        {
            'name': 'Name to Population',
            'qfmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="city">{{City}}</div>
                    <hr>
                    <p class="prompt">Ballpark population?</p>
                    <p class="sub-prompt">Nearest million if >2M otherwise to the 100k</small>
                ''',
            'afmt':
                '''
                    <div id="region">{{Region}}</div>
                    <div id="city">{{City}}</div>
                    <hr>
                    <p class="prompt">Ballpark population?</p>
                    <p class="sub-prompt">Nearest million if >2M otherwise to the 100k</small>
                    <hr>
                    <div class="population">{{PopulationReadable}}</div>
                    <hr>
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
        #city {
            font-size: 24px;
        }
        #type {
            color: gray;
            font-size: 20px;
        }
        #capital,
        #population {
            font-size: 24px;
        }
        .prompt {
            font-size: 24px;
        }
        .sub-prompt {
            color: gray;
            font-size: 20px;
        }
    ''',
)


class RegionCityNote(genanki.Note):
    @property
    def guid(self):
        """
        GUID for the note is calculated based on the wikidata ID of the corresponding page
        """
        # TODO arguably should put these at front for ultimate durability.
        # including model id, for uniqueness between models (see: https://github.com/kerrickstaley/genanki/issues/61)
        return genanki.guid_for(self.model.model_id, self.fields[8], self.fields[9])


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
    parser.add_argument('--cities', default=False, action="store_true", help="Run in cities mode. TKTK")
    parser.add_argument('--language', default='en', help="Language of the generated deck")
    parser.add_argument('--image-folder', default=None, help="Folder to store images, new temporary folder by default")
    parser.add_argument('--log-level', default='INFO', choices=['FATAL', 'ERROR', 'WARN', 'INFO', 'DEBUG'])
    args = parser.parse_args(argv[1:])

    logger.setLevel(args.log_level)

    region = CLIENT.get(args.region, load=True)
    region_label = try_get_label_in(region, args.language)
    logger.info(f'Building {"cities" if args.cities else "subdivisions"} deck for {region_label}')

    image_folder = args.image_folder
    if image_folder is not None:
        if not os.path.exists(image_folder):
            os.mkdir(image_folder)
        context_manager = nullcontext(image_folder)
    else:
        context_manager = tempfile.TemporaryDirectory()

    with context_manager as image_folder:
        logger.debug(f'Image folder {image_folder}')

        # TODO consider backing this out a bit so it's not breaking old subdivisions stuff
        # and could theoretically be merged.
        hash_material = '--'.join([
            args.region,
            args.language,
            'cities' if args.cities else 'subdivisions',
        ])
        region_hash = hashlib.sha512(hash_material.encode('utf-8')).digest()
        region_hashsum = np.frombuffer(region_hash, dtype=np.int32).sum()
        possible_ids = range(1 << 30, 1 << 31)
        deck_id = possible_ids[(DECK_ID_BASE + region_hashsum) % len(possible_ids)]

        # if mode is cities
        if args.cities:
            cities = get_top_cities(args.region)

            deck_name = f'Cities of {region_label}'
            deck = genanki.Deck(deck_id, deck_name)

            # TODO arguably this map should have all the cities passed to it somehow
            # so it has an extent that contains all of them.
            # otherwise, they city in question might not even be on the map if we prompt using it?
            region_file_name = f'{image_folder}/{args.region}-for-cities.png'
            make_map(region.get(ISO2), filename=region_file_name)

            media_files = [region_file_name]
            
            # for cities, we can do this in one pass
            for city_dict in cities:
                city = CLIENT.get(city_dict['city_id'], load=True) 
                city_label = try_get_label_in(city, args.language)
                logger.debug(f'Making map for {city.id}, {city_label}')
                coordinates = city.get(COORDINATES)
                file_name = f'{image_folder}/{args.region}-{city_dict["city_id"]}.png'
                make_map(region.get(ISO2), coordinates.latitude, coordinates.longitude, file_name)

                city_label = try_get_label_in(city, args.language)
                media_files.append(file_name)
                logger.debug(f'Building cards for {city.id}, {city_label}')

                superdivision_label = None
                superdivision = get_current_superdivision(city)
                if superdivision:
                    superdivision_label = try_get_label_in(superdivision, args.language)
                    logger.debug(f'Superdivision: {superdivision.id}, {superdivision_label}')

                unreadable_population = city_dict['population']
                readable_population = make_legible_number(unreadable_population)

                deck.add_note(
                    RegionCityNote(
                        model=REGION_CITY_MODEL,
                        fields=[
                            city_label,
                            region_label,
                            superdivision_label,
                            str(unreadable_population),
                            str(readable_population),
                            str(city_dict['rank']),
                            f'<img src="{os.path.basename(file_name)}">',
                            f'<img src="{os.path.basename(region_file_name)}">',
                            city.id,
                            args.language,
                            try_get_wikilink_in(city, args.language),
                            str(coordinates.latitude),
                            str(coordinates.longitude),
                        ]
                    )
                )

        else:
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
        logger.info(f'Wrote {len(media_files) - 1} cards to "{deck_name}.apkg"')


# other TODOs
# - add cities documentation
# - make sure i haven't broken subdivisions stuff (i currently have)
# - fully make sure it's upgradeable

if __name__ == '__main__':
    import sys

    main(sys.argv)
