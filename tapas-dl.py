#!/bin/env python3
from pyquery import PyQuery as pq
from pathlib import Path
import os
import argparse
import re
import time
import requests
import http.cookiejar
import img2pdf
from urllib.parse import urlparse


def lead0(num, max):
    return str(num).zfill(len(str(max)))


def terminal_size():
    try:
        import fcntl
        import termios
        import struct

        th, tw, hp, wp = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0)))
    except (IOError, ModuleNotFoundError):
        th, tw = 80, 200
    return tw, th


def printLine(msg='', noNewLine=False):
    terminalWidth = terminal_size()[0]
    spaces = terminalWidth - len(msg)

    if noNewLine:
        if args.verbose:
            print(' ' + msg + (' ' * (spaces - 1)))
        else:
            print(msg + (' ' * spaces), end='\r')
    else:
        print(msg + (' ' * spaces))


def file_extension(url):
    # Only look at the path, the signed query string can contain dots too
    return os.path.splitext(urlparse(url).path)[1][1:]


def check_path(path, slash=True, fat=False):
    evil_chars = []
    if slash:
        evil_chars.append('/')
    if fat:
        evil_chars += ['?', '<', '>', '\\', ':', '*', '|', '"', '^']
    return ''.join([char for char in path if char not in evil_chars])


def image_ok(data):
    # img2pdf can only embed JPEG and PNG data as it is
    return data.startswith(b'\xff\xd8') or data.startswith(b'\x89PNG')


RETRY_STATUS_CODES = (403, 429, 500, 502, 503, 504)


def fetch(url, what):
    # Wait before every request. If tapas refuses or rate limits us, back off and try again.
    # A 403 is retried without a limit, the wait grows up to 10 minutes between tries.
    attempt = 0
    while True:
        time.sleep(args.delay)
        try:
            response = s.get(url, timeout=60)
            if response.status_code not in RETRY_STATUS_CODES:
                return response
            reason = 'HTTP {}'.format(response.status_code)
            forbidden = response.status_code == 403
        except requests.RequestException as e:
            response = None
            reason = str(e)
            forbidden = False
        if not forbidden and attempt >= args.retries:
            return response
        wait = min(5 * 2 ** attempt, 600)
        printLine('{}: {}, retrying in {}s...'.format(what, reason, wait))
        time.sleep(wait)
        attempt += 1


def episode_page(episodeId, what):
    response = fetch(f'https://tapas.io/episode/{episodeId}', what)
    if response is None or response.status_code != 200:
        return None
    return pq(response.content)


def load_episode_list(seriesId):
    data = []
    episodePage = 1
    while True:
        # Large max_limit values (e.g. 99999999) make the API answer with HTTP 500, so page through it instead
        response = fetch(f'https://tapas.io/series/{seriesId}/episodes?page={episodePage}&sort=OLDEST&max_limit=20', 'Episode list')
        if response is None or response.status_code != 200:
            return None
        try:
            episodes = response.json()['data']
        except ValueError:
            return None
        page = pq(episodes['body'])
        for episode in page('[data-permalink*="/episode/"]'):
            data.append({'id': int(episode.attrib['data-permalink'][episode.attrib['data-permalink'].rfind('/') + 1:])})
        if not episodes['pagination']['has_next']:
            return data
        episodePage += 1


# parse input and settup help
parser = argparse.ArgumentParser(description='Downloads Comics from \'https://tapas.io\'.\nIf folder of downloaded comic is found, it will only download the episodes that are missing (can be disabled with -f/--force).', formatter_class=argparse.RawTextHelpFormatter)
parser.add_argument('url', metavar='URL/name', type=str, nargs='+',
                    help='URL or URL name to comic\nGo to the comic you want to download (any page)\nRightclick on the comic name in the upper left corner and select "Copy linkaddress" (Or similar) or just use the name behind series in the url\nExamples: https://tapas.io/series/Erma, RavenWolf, ...')
parser.add_argument('-f', '--force', action="store_true", help='Disables updater.')
parser.add_argument('-v', '--verbose', action="store_true", help='Enables verbose mode.')
parser.add_argument('-r', '--restrict-characters', action="store_true", help='Removes \'? < > \\ : * | " ^\' from file names')
parser.add_argument('-c', '--cookies', type=str, nargs='?', default="", dest='cookies', metavar='PATH',
                    help='Optional cookies.txt file to load, can be used to allow the script to "log in" and circumvent age verification.')
parser.add_argument('-o', '--output-dir', type=str, nargs='?', default="", dest='baseDir', metavar='PATH',
                    help='Output directory where comics should be placed.\nIf left blank, the script folder will be used.')
parser.add_argument('-d', '--delay', type=float, default=0.5, dest='delay', metavar='SECONDS',
                    help='Seconds to wait before each request to tapas.io (default: 0.5).')
parser.add_argument('-R', '--retries', type=int, default=5, dest='retries', metavar='N',
                    help='How often to retry a rate limited or failed request, waiting longer each time (default: 5). 403 errors are retried until they succeed.')

args = parser.parse_args()

s = requests.Session()
s.headers.update({'user-agent': 'tapas-dl'})
if args.cookies:
    s.cookies = http.cookiejar.MozillaCookieJar()
    s.cookies.load(args.cookies, ignore_discard=True, ignore_expires=True)
# FIXME Those cookies should be set, but no idea how...
#s.cookies.update({'birthDate': '1901-01-01'})
#s.cookies.update({'adjustedBirthDate': '1901-01-01'})

basePath = ""
if (args.baseDir):
    basePath = Path(args.baseDir)


def download_series(url, urlCount, urlTotal):
    # check url/name
    url = url.rstrip('/')
    if re.match(r'^https://tapas\.io/series/.+$', url):
        urlName = url[url.rindex('/') + 1:]
    else:
        urlName = url

    printLine('Loading ' + urlName + '...', True)

    # Get comic start page and test if comic exsists
    pageReqest = fetch('https://tapas.io/series/' + urlName, 'Series page')

    if pageReqest is None or pageReqest.status_code != 200:
        status = 'no response' if pageReqest is None else 'HTTP {}'.format(pageReqest.status_code)
        printLine('Error: Comic "{}" could not be loaded ({})\n'.format(urlName, status))
        return False

    page = pq(pageReqest.text)

    name = page('.center-info__title.center-info__title--small').text()

    author = page('div.viewer-section.viewer-section--episode a.name').text()

    seriesId = page('.subscribe-btn').attr('data-id')

    if len(page('.row-item--info > img')) > 0:
        headerSrc = page('.row-item--info > img').attr('src')
    else:
        headerSrc = None

    data = load_episode_list(seriesId)
    if data is None:
        printLine('Error: Could not load the episode list of "{}", try again later\n'.format(urlName))
        return True

    printLine('{} [{}] ({} pages):'.format(name, urlName, len(data)))

    # Check if folder exsists, if not create it
    printLine('Checking folder...', True)
    # If the user specified a base output directory, prepend that on our folder
    savePath = check_path('{} [{}]'.format(name, urlName), fat=args.restrict_characters)
    if (basePath != ""):
        savePath = os.path.join(basePath, savePath)
        printLine('Full path is: ' + str(savePath))
    if os.path.isdir(savePath) and not args.force:
        printLine('Found directory, only downloading missing episodes (use -f/--force to disable)')
    elif not os.path.isdir(savePath):
        os.makedirs(savePath)
        printLine('Creating folder...', True)

    # Download header
    if True not in [file.name.startswith('-1 - header.') for file in os.scandir(savePath)]:
        printLine('Downloading header...', True)

        if headerSrc is not None:
            response = fetch(headerSrc, 'Header')
            if response is not None and response.status_code == 200:
                with open(os.path.join(savePath, '-1 - header.{}'.format(file_extension(headerSrc))), 'wb') as f:
                    f.write(response.content)

                printLine('Downloaded header')
            else:
                printLine('Error: Header could not be downloaded')
        else:
            printLine('Header not found')

    if len(data) <= 0:
        print('Nothing todo: No pages found\n')
        return True

    # Check if series is comic or novel, based on the first episode that loads
    kind = None
    for episode in data:
        epPage = episode_page(episode['id'], 'Checking episode type')
        if epPage is None:
            continue
        if len(epPage('.content__img.js-lazy')) > 0:
            kind = 'comic'
        elif len(epPage('article.viewer__body > div.ep-epub-content p')) > 0:
            kind = 'novel'
        if kind is not None:
            break

    if kind is None:
        printLine('Error: Could not tell if "{}" is a comic or a novel, no episode could be loaded. Try again later.\n'.format(urlName))
        return True

    if kind == 'comic':
        printLine('Detected comic')

        # Episodes that already have a PDF are skipped, unless -f/--force is used
        done = set()
        if not args.force:
            for fileName in os.listdir(savePath):
                match = re.search(r'#(\d+)\.pdf$', fileName)
                if match:
                    done.add(int(match.group(1)))

        saved = 0
        failed = []
        for index, episode in enumerate(data, start=1):
            episodeId = episode['id']
            if episodeId in done:
                continue

            epPage = episode_page(episodeId, 'Episode {}/{}'.format(index, len(data)))
            if epPage is None:
                failed.append((index, episodeId, 'page could not be loaded'))
                continue

            title = epPage('.js-ep-title').text() or 'Episode {}'.format(index)

            imgUrls = [pq(img).attr('data-src') for img in epPage('.content__img')]
            if not imgUrls:
                if 'Unlock episode' in epPage.html():
                    failed.append((index, episodeId, 'locked, unlock it on tapas.io first'))
                else:
                    failed.append((index, episodeId, 'no images on the page'))
                continue

            images = []
            problem = None
            for imgCount, imgUrl in enumerate(imgUrls, start=1):
                printLine('Downloading episode {}/{}, image {}/{}...'.format(index, len(data), imgCount, len(imgUrls)), True)
                response = fetch(imgUrl, 'Episode {}/{}, image {}'.format(index, len(data), imgCount))
                if response is None or response.status_code != 200:
                    problem = 'image {} could not be loaded'.format(imgCount)
                    break
                if not image_ok(response.content):
                    problem = 'image {} is not JPEG or PNG'.format(imgCount)
                    break
                images.append(response.content)
            if problem is not None:
                failed.append((index, episodeId, problem))
                continue

            pdfPath = os.path.join(savePath, check_path('{} - {} - #{}.pdf'.format(lead0(index, len(data)), title, episodeId), fat=args.restrict_characters))
            # Write to a temporary name first, so an interrupted download is not counted as done
            with open(pdfPath + '.part', 'wb') as f:
                f.write(img2pdf.convert(images))
            os.replace(pdfPath + '.part', pdfPath)

            saved += 1
            printLine('Saved {} ({} pages)'.format(os.path.basename(pdfPath), len(images)))

        printLine('Saved {} new episodes, {} already there'.format(saved, len(done)))
        for index, episodeId, problem in failed:
            printLine('Not downloaded: episode {}/{} (#{}): {}'.format(index, len(data), episodeId, problem))
        if failed:
            printLine('Run the same command again to retry them.')
    else:
        printLine('Detected novel')

        from ebooklib import epub

        # download/create epub
        book = epub.EpubBook()

        customCss = None

        # Add meta data
        book.set_identifier(urlName)
        book.set_title(name)
        book.set_language('en')

        book.add_author(author)

        headerFiles = [file for file in os.listdir(savePath) if re.match(r'.+header\..+', file)]
        if headerFiles:
            book.set_cover("cover.jpg", open(os.path.join(savePath, headerFiles[0]), 'rb').read())

        book.toc = []
        book.spine = ['cover']

        # create about page
        chapter = epub.EpubHtml(title='about', file_name='about.xhtml')
        chapter.content = f'<h1>About</h1><p>Title: {name}</p><p>Author: {book.metadata["http://purl.org/dc/elements/1.1/"]["creator"][0][0]}</p><p>Source: <a href="{"https://tapas.io/series/" + urlName}">{"https://tapas.io/series/" + urlName}</a></p>'

        book.add_item(chapter)
        book.spine.append(chapter)

        # Append nav page
        book.spine.append('nav')

        # create chapters
        for pageCount, pageData in enumerate(data):
            printLine('Downloaded page {}/{}...'.format(pageCount + 1, len(data)), True)

            pagePq = episode_page(pageData['id'], 'Episode {}/{}'.format(pageCount + 1, len(data)))
            if pagePq is None:
                printLine('Error: Episode {}/{} (#{}) could not be loaded, it is missing from the novel'.format(pageCount + 1, len(data), pageData['id']))
                continue
            pageTitle = pagePq('.viewer__header > .title').text()

            pageHtml = f'<h1>{pageTitle}</h1>'
            for p in pagePq('article.viewer__body > div.ep-epub-content p'):
                p = pq(p)
                if p.text() is not None:
                    pageHtml += '<p>' + p.text() + '</p>'

            chapter = epub.EpubHtml(title=pageTitle, file_name=str(pageData['id']) + '.xhtml')
            chapter.content = pageHtml

            book.add_item(chapter)

            # define Table Of Contents
            book.toc.append(epub.Link(str(pageData['id']) + '.xhtml', pageTitle, str(pageData['id'])))

            # basic spine
            book.spine.append(chapter)

        # add default NCX and Nav file
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        # add CSS
        style = ''
        nav_css = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css", content=style)
        book.add_item(nav_css)

        # write to the file, the folder with the cover is kept
        epub.write_epub(os.path.join(os.path.dirname(savePath), check_path(f'{name}.epub', fat=args.restrict_characters)), book)

    if urlCount + 1 != urlTotal:
        printLine()
    return True


for urlCount, url in enumerate(args.url):
    if not download_series(url, urlCount, len(args.url)):
        break
