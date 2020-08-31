from __future__ import absolute_import, division, print_function

from beets import plugins, ui, util, config
from beets.ui.commands import PromptChoice
from beets.autotag import hooks, mb

from collections import namedtuple, defaultdict

_artist_cache = {}


ArtistNameAlts = namedtuple("ArtistNameAlts",
                        ("canon_name", "alias_name", "credited_name", "user_chosen_name"))

ArtistReleaseData = namedtuple("ArtistReleaseData",
                               ("tracks", "albums", "name_choice", "aliases"))

AlbumNameHoles = namedtuple("AlbumNameHoles",
                            ("album", "tracks"))

BROWSE_INCLUDES = ['artist-credits', 'aliases']

class MbArtistCreditPlugin(plugins.BeetsPlugin):
    def __init__(self):
        super(MbArtistCreditPlugin, self).__init__()

        self.register_listener('albuminfo_received', self.album_info_handler)
        self.register_listener('trackinfo_received', self.track_info_handler)
        # self.register_listener('before_choose_candidate', self.before_choice_handler)
        self.register_listner('import_task_choice', self.import_task_choice_handler)

        # A set of artists for which the user needs to choose a preferred name for
        # album_id/track_id: set(artist_id)
        self.choices = defaultdict(set)

        # Set of name "holes" used to recover where in the info object names need to be replaced
        # for an album
        # album_id: AlbumNameHoles
        self.album_holes = {}

        # A store of acquired data on an artist, cache to avoid repeat lookups
        # artist_id: ArtistReleaseData
        self.artists_data = {}


    def _fetch_artist_release_data(artist_id):
        """ Fetch artist releases data for a given artist_id, from musicbrainz,
        and cache the result
        """
        offset = 0
        LIMIT = 100
        releases = []
        while True:
            res = musicbrainzngs.browse_releases(artist=artist, BROWSE_INCLUDES, limit=LIMIT, offset=offset)
            res = res.get('release-list', [])

            offset += LIMIT
            releases.append(res)

            if len(res) != LIMIT:
                break

        albums = {}
        aliases = None

        # Construct releases dict
        for rel in releases:
            album_id = rel['id']
            albums[album_id] = rel

        # Acquire artist alias-list
        for rel in releases:
            for cred in rel.get('artist-credit', []):
                if cred['artist']['id'] == artist_id:
                    aliases = cred['artist'].get('alias-list')
                    break
            #Break to outer loop if inner is broken
            else:
                continue
            break

        artist_data = ArtistReleaseData({}, albums, None, aliases)
        return artist_data




    def album_info_handler(self, info):
        self._log.debug(u'Recieved albuminfo.')

        album_id = info['album_id']
        artist_id = info['artist_id']

        # Skip if there's no musicbrainz id
        if album_id is None or artist_id is None:
            return

        # Check cache for existing data for this album
        artist_data = self.artists_data.get(artist_id)

        if artist_data is None:
            artist_data = self._fetch_artist_release_data(artist_id)

        # Should always have album data if we have artist_data...
        album_data = artist_data.albums.get(album_id)
        if album_data is None:
            return # Should skip or throw an Exception?

        # Acquire list of credited artists, and construct album name holes
        credited_artists = []
        holes = AlbumNameHoles(album=None, tracks={})
        album_credit = album_data['artist-credit']
        credited_artists.append(album_credit)
        holes.album = album_credit

        for track in _iter_tracks(album_data):
            track_cred = track.get('artist-credit')
            if track_cred is None:
                continue
            holes.tracks[track['id']] = track_cred
            credited_artists.extend(track_cred)

        self.album_holes[album_id] = holes

        # Determine name choices that need to be made
        for cred in credited_artists:
            # Skip string credit parts (and, ft., x, etc)
            if isinstance(el, six.string_types):
                continue
            cred_artist_id = cred['artist']['id']
            # Skip artists we've done already
            cred_artist_data = self.artists_data.get(cred_artist_id)
            if (cred_artist_data is not None) and (cred_artist_data.name_choice is not None):
                continue

            # Fetch artist musicbrainz release data if we need to
            if cred_artist_data is None:
                cred_artist_data = self._fetch_artist_release_data(cred_artist_id)

            canon_name =  cred['artist']['name']
            alias_name = mb._preferred_alias(cred_artist_data.aliases)
            fallback_name = _artist_credit_fallback_alias(cred_artist_id)

            self.artists_data[cred_artist_id].name_choice = ArtistNameAlts(canon_name, alias_name, credit_name, None)
            self.choices[album_id].append(cred_artist_id)


    def track_info_handler(self, info):
        self._log.debug(u'Recieved trackinfo: {0}', info)

    def import_task_choice_handler(self, session, task):
        self._log.debug(u'Recieved import_stage: {0}', task)
        if not task.is_album:
            return
        album_id = task.album.album_id
        if album_id is None:
            return

        artist_choices = self.choices[album_id]
        for artist_id in artist_choices:
            alts = self.artist_data[artist_id].name_choice
            if alts.user_choice is not None:
                continue

            user_choices = self._get_choices(alts)

            if len(user_choices) == 1:
                self.artist_data[artist_id].name_choice.user_choice = user_choices[0][0]
                continue

            opt_num = 0
            msg = "Multiple Possible Alternative Names for {name}:".format(name=alts.canon_name)
            ui._print(msg)

            for name, prompt in user_choices:
                opt_num += 1
                name_print = ui.colorize('action', name)
                option_print = "{num}. {prompt}: {name}".format(name=name_print,
                                                                num=opt_num,
                                                                prompt=prompt)
                ui.print_(option_print)

            choice_prompt = "Please choose a name (as numbered)."
            response = ui.input_options([],
                                        prompt=choice_prompt,
                                        numrange=(1, opt_num))

            self.artist_data[artist_id].name_choice.user_choice = user_choice[int(response)][1]

        self._apply_choices(task)
        # TODO: Figure out what plugin data we can throwaway here... (oops)

    def _get_name(self, cred):
        if isinstance(cred, six.string_types):
            return cred

        artist_id = cred['artist']['id']
        name = self.artists_data[artist_id].name_choice.user_choice
        return name

    def _apply_choices(self, task):
        album_id = task.album.album_id
        holes = self.album_holes[album_id]

        album_artist_name = ''.join([self._get_name(x) for x in holes.album])
        track_artists = {}

        # Construct the track names in advance
        for track_id, track_creds in holes.tracks.items():
            track_artist = ''.join([self._get_name(x) for x in track_creds])
            track_artists[track_id] = track_name

        # Apply the album artist name
        task.album.artist = album_artist_name

        # Apply the track artist names
        for track in task.album.tracks:
            track_id = track.release_track_id
            track.artist = track_names[track_id]

    def _get_choices(self, name_alts):
        """ Constructs the name choices to offer to the user
        as a list of tuples of the names, and a description of the name
        to show the user when offering the choice
        (dependent on config)
        """
        choices = []
        choices.append((name_alts.canon_name, "Musicbrainz Canonical Name"))
        if (name_alts.alias_name is not None) and \
           (name_alts.alias_name != name_alts.canon_name):
            choices.append((name_alts.alias_name, "Localised Alias"))

        if (name_alts.credited_name is not None) and \
           (name_alts.credited_name != name_alts.canon_name) and \
           (name_alts.credited_name != name_alts.alias_name):
            choies.append((name_alts.credited_name, "Artist Credited Name"))

_ISO_639_1_TO_3 = {
    'aa': 'aar',
    'ab': 'abk',
    'ae': 'ave',
    'af': 'afr',
    'ak': 'aka',
    'am': 'amh',
    'an': 'arg',
    'ar': 'ara',
    'as': 'asm',
    'av': 'ava',
    'ay': 'aym',
    'az': 'aze',
    'ba': 'bak',
    'be': 'bel',
    'bg': 'bul',
    'bi': 'bis',
    'bm': 'bam',
    'bn': 'ben',
    'bo': 'bod',
    'br': 'bre',
    'bs': 'bos',
    'ca': 'cat',
    'ce': 'che',
    'ch': 'cha',
    'co': 'cos',
    'cr': 'cre',
    'cs': 'ces',
    'cu': 'chu',
    'cv': 'chv',
    'cy': 'cym',
    'da': 'dan',
    'de': 'deu',
    'dv': 'div',
    'dz': 'dzo',
    'ee': 'ewe',
    'el': 'ell',
    'en': 'eng',
    'eo': 'epo',
    'es': 'spa',
    'et': 'est',
    'eu': 'eus',
    'fa': 'fas',
    'ff': 'ful',
    'fi': 'fin',
    'fj': 'fij',
    'fo': 'fao',
    'fr': 'fra',
    'fy': 'fry',
    'ga': 'gle',
    'gd': 'gla',
    'gl': 'glg',
    'gn': 'grn',
    'gu': 'guj',
    'gv': 'glv',
    'ha': 'hau',
    'he': 'heb',
    'hi': 'hin',
    'ho': 'hmo',
    'hr': 'hrv',
    'ht': 'hat',
    'hu': 'hun',
    'hy': 'hye',
    'hz': 'her',
    'ia': 'ina',
    'id': 'ind',
    'ie': 'ile',
    'ig': 'ibo',
    'ii': 'iii',
    'ik': 'ipk',
    'io': 'ido',
    'is': 'isl',
    'it': 'ita',
    'iu': 'iku',
    'ja': 'jpn',
    'jv': 'jav',
    'ka': 'kat',
    'kg': 'kon',
    'ki': 'kik',
    'kj': 'kua',
    'kk': 'kaz',
    'kl': 'kal',
    'km': 'khm',
    'kn': 'kan',
    'ko': 'kor',
    'kr': 'kau',
    'ks': 'kas',
    'ku': 'kur',
    'kv': 'kom',
    'kw': 'cor',
    'ky': 'kir',
    'la': 'lat',
    'lb': 'ltz',
    'lg': 'lug',
    'li': 'lim',
    'ln': 'lin',
    'lo': 'lao',
    'lt': 'lit',
    'lu': 'lub',
    'lv': 'lav',
    'mg': 'mlg',
    'mh': 'mah',
    'mi': 'mri',
    'mk': 'mkd',
    'ml': 'mal',
    'mn': 'mon',
    'mr': 'mar',
    'ms': 'msa',
    'mt': 'mlt',
    'my': 'mya',
    'na': 'nau',
    'nb': 'nob',
    'nd': 'nde',
    'ne': 'nep',
    'ng': 'ndo',
    'nl': 'nld',
    'nn': 'nno',
    'no': 'nor',
    'nr': 'nbl',
    'nv': 'nav',
    'ny': 'nya',
    'oc': 'oci',
    'oj': 'oji',
    'om': 'orm',
    'or': 'ori',
    'os': 'oss',
    'pa': 'pan',
    'pi': 'pli',
    'pl': 'pol',
    'ps': 'pus',
    'pt': 'por',
    'qu': 'que',
    'rm': 'roh',
    'rn': 'run',
    'ro': 'ron',
    'ru': 'rus',
    'rw': 'kin',
    'sa': 'san',
    'sc': 'srd',
    'sd': 'snd',
    'se': 'sme',
    'sg': 'sag',
    'sh': 'hbs',
    'si': 'sin',
    'sk': 'slk',
    'sl': 'slv',
    'sm': 'smo',
    'sn': 'sna',
    'so': 'som',
    'sq': 'sqi',
    'sr': 'srp',
    'ss': 'ssw',
    'st': 'sot',
    'su': 'sun',
    'sv': 'swe',
    'sw': 'swa',
    'ta': 'tam',
    'te': 'tel',
    'tg': 'tgk',
    'th': 'tha',
    'ti': 'tir',
    'tk': 'tuk',
    'tl': 'tgl',
    'tn': 'tsn',
    'to': 'ton',
    'tr': 'tur',
    'ts': 'tso',
    'tt': 'tat',
    'tw': 'twi',
    'ty': 'tah',
    'ug': 'uig',
    'uk': 'ukr',
    'ur': 'urd',
    'uz': 'uzb',
    've': 'ven',
    'vi': 'vie',
    'vo': 'vol',
    'wa': 'wln',
    'wo': 'wol',
    'xh': 'xho',
    'yi': 'yid',
    'yo': 'yor',
    'za': 'zha',
    'zh': 'zho',
    'zu': 'zul'}

def _artist_credit_fallback_alias(artist):
    """Given an artist block, attempt to find an alias for the artist with the user's preferred locale
    from all the artist-credits for the artist.
    Optionally, we may have data for a release already, so we can try it first.

    Returns an alias for the artist, or None if one is not found
    """

    expired_aliases = set()
    for alias in artist.get('alias-list', []):
        if 'end-date' in alias:
            expired_aliases.add(alias['alias'])

    # Comparing by ISO639-3, which musicbrainz seems to use for release languages (but not for aliases!)
    preferred_langs = [_ISO_639_1_TO_3.get(x, x) for x in config['import']['languages']]

    # Lookup additional data on how the artist is credited on their releases
    # This could perhaps be cached somehow, if repeated api calls are a problem...
    # (Even just a cache of the last artist would make a difference in this case)
    try:
        res = wrap_browse_artist(artist=artist['id'], includes='artist-credits')
    except musicbrainzngs.ResponseError:
        return None

    releases = res['release-list']

    # Look through artist releases to find an artist-credits matching the users
    # preferred languages
    name_candidates = defaultdict(list)
    for release in releases:
        release_lang = release.get('text-representation', {}).get('language')
        release_lang = _ISO_639_1_TO_3.get(release_lang, release_lang)

        # Skip releases without a language
        if release_lang is None:
            continue

        # Don't use data from bootleg releases
        if release.get('status') == 'Bootleg':
            continue

        # Skip bad quality data
        if release.get('quality') != 'normal':
            continue

        if release_lang in preferred_langs:
            # Get artist-credit name matching artist-id
            credit_name = None
            for credit in release['artist-credit']:
                # Skip over credits like x, ft., and, etc
                if (isinstance(credit, six.string_types)):
                    continue
                if credit['artist']['id'] == artist['id']:
                    # Differing credit name is under credit['name'] otherwise, under credit['artist']['name']
                    credit_name = credit.get('name', credit['artist']['name'])
                    if credit_name is None:
                        continue

                    if credit_name in expired_aliases:
                        credit_name = None
                        continue

                    credit_date_str = release.get('date')
                    if credit_date_str is not None:
                        # Simple object to pass to _set_date_str()
                        credit_date = lambda x: None
                        credit_date.year = -1
                        credit_date.month = -1
                        credit_date.day = -1
                        _set_date_str(credit_date, credit_date_str, False)
                        # Date tuple comparable with >
                        credit_date = (credit_date.year, credit_date.month, credit_date.day)
                    else:
                        # Sort last if there is no date
                        credit_date = (-1,-1,-1)
                    break;

            if credit_name is None:
                continue;

            name_candidates[release_lang].append({"name": credit_name, "date": credit_date})


    # Choose from found credit-names based on preferred languages
    for lang in preferred_langs:
        names = name_candidates.get(lang)
        if names:
            names.sort(key=lambda x: x["date"], reverse=True)
            return names[0]['name']

    # Nothing valid found, fallback to what we had already
    return None

#(copied parts of beets/autotag/mb.py albuminfo, since there's no plugin hook or util for this)
def _iter_tracks(release):
""" Iterates over track data in a musicbrainz release dict, yielding each track
"""
    for medium in release['medium-list']:
        disctitle = medium.get('title')
        format = medium.get('format')

        if format in config['match']['ignored_media'].as_str_seq():
            continue

        all_tracks = medium['track-list']
        if ('data-track-list' in medium
                and not config['match']['ignore_data_tracks']):
            all_tracks += medium['data-track-list']
        track_count = len(all_tracks)

        if 'pregap' in medium:
            all_tracks.insert(0, medium['pregap'])

        for track in all_tracks:

            if ('title' in track['recording'] and
                    track['recording']['title'] in SKIPPED_TRACKS):
                continue

            if ('video' in track['recording'] and
                    track['recording']['video'] == 'true' and
                    config['match']['ignore_video_tracks']):
                continue

            yield track
