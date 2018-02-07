#!/usr/bin/env python

"""
Sushi Chef for http://3asafeer.com/
We make an HTML5 app out of each interactive reader.
"""

from collections import defaultdict
import html
import os
import re
import requests
import tempfile
import time
from urllib.parse import urlparse, parse_qs
import uuid

from bs4 import BeautifulSoup

import le_utils.constants
from ricecooker.chefs import SushiChef
from ricecooker.classes import nodes, files, licenses
from ricecooker.utils.caching import CacheForeverHeuristic, FileCache, CacheControlAdapter, InvalidatingCacheControlAdapter
from ricecooker.utils.browser import preview_in_browser
from ricecooker.utils.html import download_file, WebDriver
from ricecooker.utils.zip import create_predictable_zip
import selenium.webdriver.support.ui as selenium_ui
from distutils.dir_util import copy_tree


sess = requests.Session()
cache = FileCache('.webcache')
forever_adapter = CacheControlAdapter(heuristic=CacheForeverHeuristic(), cache=cache)

sess.mount('http://3asafeer.com/', forever_adapter)
sess.mount('http://fonts.googleapis.com/', forever_adapter)


headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 6.1; WOW64; rv:20.0) Gecko/20100101 Firefox/20.0",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive"
}


class ThreeAsafeerChef(SushiChef):
    """
    The chef class that takes care of uploading channel to the content curation server.

    We'll call its `main()` method from the command line script.
    """
    channel_info = {
        'CHANNEL_SOURCE_DOMAIN': "3asafeer.com",
        'CHANNEL_SOURCE_ID': "3asafeer",
        'CHANNEL_TITLE': "3asafeer",
        'CHANNEL_THUMBNAIL': "thumbnail.png",
        'CHANNEL_DESCRIPTION': "An online digital library for reading in Arabic with pictures and audio.",
    }

    def construct_channel(self, **kwargs):
        """
        Create ChannelNode and build topic tree.
        """
        # create channel
        channel_info = self.channel_info
        channel = nodes.ChannelNode(
            source_domain = channel_info['CHANNEL_SOURCE_DOMAIN'],
            source_id = channel_info['CHANNEL_SOURCE_ID'],
            title = channel_info['CHANNEL_TITLE'],
            thumbnail = channel_info.get('CHANNEL_THUMBNAIL'),
            description = channel_info.get('CHANNEL_DESCRIPTION'),
            language = "ar",
        )

        download_all(channel)
        return channel


def download_all(channel):
    print("Getting number of books")
    books_count = get_books_count()
    print("There are %s books ... scraping them now!" % books_count)

    for i in range(books_count):
        print()
        print('-' * 80)
        print('Downloading book %s of %s' % (i, books_count))
        channel.add_child(download_single(i))


def get_books_count():
    with WebDriver("http://3asafeer.com/", delay=3000) as driver:
        click_read_and_wait(driver)
        return len(driver.find_elements_by_css_selector('.story-cover'))


def click_read_and_wait(driver):
    read_link = driver.find_element_by_css_selector('#readLink')
    read_link.click()
    selenium_ui.WebDriverWait(driver, 60).until(
            lambda driver: driver.find_element_by_id('list-container'))
    time.sleep(3)


def download_single(i):
    """Download the book at index i."""
    with WebDriver("http://3asafeer.com/", delay=3000) as driver:

        print('Closing popup')
        close_popup = driver.find_element_by_css_selector('.fancybox-item.fancybox-close')
        close_popup.click()
        time.sleep(1)

        print('Clicking "read"')
        click_read_and_wait(driver)

        book = driver.find_elements_by_css_selector('.story-cover')[i]
        book_id = book.get_attribute('id')
        cover_src = book.find_element_by_css_selector('.cover').get_attribute('src')
        thumbnail = make_fully_qualified_url(cover_src)
        title = book.find_element_by_css_selector('.cover-title').text

        print('Clicking book %s' % book_id)
        link = book.find_element_by_css_selector('.story')
        link.click()

        try:
            selenium_ui.WebDriverWait(driver, 30).until(
                    lambda driver: driver.find_element_by_id('reader-viewport'))
        except:
            print("Not able to click into the book :(, check screenshot.png")
            driver.save_screenshot('screenshot.png')
            raise

        time.sleep(5)

        doc = BeautifulSoup(driver.page_source, "html.parser")
        return process_node_from_doc(doc, book_id, title, thumbnail)


def process_node_from_doc(doc, book_id, title, thumbnail):
    """Extract a Ricecooker node given the HTML source and some metadata."""
    # Create a temporary folder to download all the files for a book.
    destination = tempfile.mkdtemp()

    # Download all the JS/CSS/images/audio/etc. we'll need to make a standalone
    # app.
    doc = download_static_assets(doc, destination)

    # Remove a bunch of HTML that we don't want showing in our standalone app.
    doc.select_one('base')['href'] = ''
    remove_node('#loading')
    remove_node('#finishedActions')
    remove_node('.bookmarkbtn')
    remove_node('.reader-expand')
    remove_node('#progressBar')
    remove_node('#androidNotification')
    remove_node('#exit')

    # Write out the HTML source.
    with open(os.path.join(destination, "index.html"), "w") as f:
        f.write(str(doc))

    # Ensure the thumbnail is in a format Ricecooker can accept, and if not,
    # use the first slide as the thumbnail.
    thumbnail_extensions = ('jpg', 'jpeg', 'png')
    if not thumbnail.lower().endswith(thumbnail_extensions):
        first_slide_src = doc.select_one('#slide-container .slide img')['src']
        thumbnail = make_fully_qualified_url(first_slide_src)
        if not thumbnail.lower().endswith(thumbnail_extensions):
            thumbnail = None

    print("Downloaded book %s titled \"%s\" (thumbnail %s) to destination %s" % (
        book_id, title, thumbnail, destination))
    #preview_in_browser(destination)

    zip_path = create_predictable_zip(destination)
    return nodes.HTML5AppNode(
        source_id=book_id,
        title=truncate_metadata(title),
        license=licenses.CC_BYLicense(
            copyright_holder=truncate_metadata('3asafeer.com')),
        thumbnail=thumbnail,
        files=[files.HTMLZipFile(zip_path)],
        language="ar",
    )


def remove_node(selector):
    node = doc.select_one(selector)
    if node:
        node.decompose()


def truncate_metadata(data_string):
    MAX_CHARS = 190
    if len(data_string) > MAX_CHARS:
        data_string = data_string[:190] + " ..."
    return data_string


CSS_URL_RE = re.compile(r"url\(['\"]?(.*?)['\"]?\)")
IMAGES_IN_JS_RE = re.compile(r"images/(.*?)['\")]")


def download_static_assets(doc, destination):
    """Download all the static assets for a given book's HTML soup.

    Will download JS, CSS, images, and audio clips.
    """
    # Helper function to download all assets for a given CSS selector.
    def download_assets(selector, attr, url_middleware=None,
            content_middleware=None, node_filter=None):
        nodes = doc.select(selector)

        for i, node in enumerate(nodes):

            if node_filter:
                if not node_filter(node):
                    src = node[attr]
                    node[attr] = ''
                    print('Skipping node with src ', src)
                    continue

            url = make_fully_qualified_url(node[attr])

            if is_blacklisted(url):
                print('Skipping downloading blacklisted url', url)
                node[attr] = ""
                continue

            if 'jquery.fancybox.pack.js' in url:
                node[attr] = "static/jquery.fancybox.dummy.js"
                continue

            if url_middleware:
                url = url_middleware(url)

            filename = derive_filename(url)
            node[attr] = filename

            print("Downloading", url, "to filename", filename)
            download_file(url, destination, request_fn=make_request,
                    filename=filename, middleware_callbacks=content_middleware)

    def js_middleware(content, url, **kwargs):
        # Download all images referenced in JS files
        for img in IMAGES_IN_JS_RE.findall(content):
            url = make_fully_qualified_url('/images/%s' % img)
            print("Downloading", url, "to filename", img)
            download_file(url, destination, subpath="images",
                    request_fn=make_request, filename=img)

        # Polyfill localStorage and document.cookie as iframes can't access
        # them
        return (content
            .replace("localStorage", "_localStorage")
            .replace('document.cookie.split', '"".split')
            .replace('document.cookie', 'window._document_cookie'))

    def css_url_middleware(url):
        # Somehow the minified app CSS doesn't render images. Download the
        # original.
        return url.replace("app.min.css", "app.css")

    def css_node_filter(node):
        return "stylesheet" in node["rel"]

    def css_content_middleware(content, url, **kwargs):
        # Download linked fonts and images
        def repl(match):
            src = match.group(1)
            if src.startswith('//localhost'):
                return 'src()'
            # Don't download data: files
            if src.startswith('data:'):
                return match.group(0)
            src_url = make_fully_qualified_url(src)
            derived_filename = derive_filename(src_url)
            download_file(src_url, destination, request_fn=make_request,
                    filename=derived_filename)
            return 'src("%s")' % derived_filename

        return CSS_URL_RE.sub(repl, content)

    # Download all linked static assets.
    download_assets("img[src]", "src")  # Images
    download_assets("link[href]", "href", url_middleware=css_url_middleware,
            content_middleware=css_content_middleware,
            node_filter=css_node_filter)  # CSS
    download_assets("script[src]", "src", content_middleware=js_middleware) # JS
    download_assets("source[src]", "src") # Audio
    download_assets("source[srcset]", "srcset") # Audio

    # ... and also run the middleware on CSS/JS embedded in the page source to
    # get linked files.
    for node in doc.select('style'):
        node.string = css_content_middleware(node.get_text(), url='')

    for node in doc.select('script'):
        if not node.attrs.get('src'):
            node.string = js_middleware(node.get_text(), url='')

    # Copy over some of our own JS/CSS files and then add links to them in the
    # page source.
    copy_tree("static", os.path.join(destination, "static"))

    chef_head_script = doc.new_tag("script", src="static/chef_end_of_head.js")
    doc.select_one('head').append(chef_head_script)

    chef_body_script = doc.new_tag("script", src="static/chef_end_of_body.js")
    doc.select_one('body').append(chef_body_script)

    chef_css = doc.new_tag("link", href="static/chef.css")
    doc.select_one('head').append(chef_css)

    return doc


url_blacklist = [
    'google-analytics.com/analytics.js',
    'fbds.js',
    'chimpstatic.com',
]

def is_blacklisted(url):
    return any((item in url) for item in url_blacklist)


def derive_filename(url):
    return "%s.%s" % (uuid.uuid4().hex, os.path.basename(urlparse(url).path))


def make_request(url, clear_cookies=True, timeout=60, *args, **kwargs):
    if clear_cookies:
        sess.cookies.clear()

    retry_count = 0
    max_retries = 5
    while True:
        try:
            response = sess.get(url, headers=headers, timeout=timeout, *args, **kwargs)
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
            retry_count += 1
            print("Error with connection ('{msg}'); about to perform retry {count} of {trymax}."
                  .format(msg=str(e), count=retry_count, trymax=max_retries))
            time.sleep(retry_count * 1)
            if retry_count >= max_retries:
                return Dummy404ResponseObject(url=url)

    if response.status_code != 200:
        print("NOT FOUND:", url)

    return response


def make_fully_qualified_url(url):
    if url.startswith("../images"):
        return "http://3asafeer.com" + url[2:]
    if url.startswith("../scripts"):
        return "http://3asafeer.com" + url[2:]
    if url.startswith("//"):
        return "http:" + url
    if url.startswith("/"):
        return "http://3asafeer.com" + url
    if not url.startswith("http"):
        return "http://3asafeer.com/" + url
    return url


if __name__ == '__main__':
    """
    This code will run when the sushi chef is called from the command line.
    """
    chef = ThreeAsafeerChef()
    chef.main()