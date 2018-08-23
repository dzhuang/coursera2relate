# -*- coding: utf-8 -*-

import os
import datetime
import sys
import jinja2
import re
from peewee import SqliteDatabase, OperationalError
from coursera.models import (
    Module, Lesson, Item, ItemVideoAsset, ItemAsset, Reference, CourseAsset, Course)
from django.conf.global_settings import LANGUAGES
from coursera.utils import BeautifulSoup
from bs4 import NavigableString
from qiniu import Auth, put_file, etag, BucketManager
import html

sys.stdout = sys.__stdout__
sys.stderr = sys.__stderr__

QINIU_BUCKET_URL_PREFIX = os.environ.get("QINIU_BUCKET_URL_PREFIX", "foo")

LOCAL_PATH_PREFIX = os.getcwd()

DB_PATH = os.path.join(LOCAL_PATH_PREFIX, "coursera-dl.db")

database = SqliteDatabase(DB_PATH)

upload_to_qiniu = False
QINIU_ACCESS_KEY = os.environ.get("QINIU_ACCESS_KEY", "")
QINIU_SECRET_KEY = os.environ.get("QINIU_SECRET_KEY", "")
QINIU_BUCKET_NAME = os.environ.get("QINIU_BUCKET_NAME", "")
IN_BUCKET_PREFIX = "coursera-videos"

qiniu_auth = None
bucket = None

if (not sys.platform.startswith("win")
        and QINIU_ACCESS_KEY and QINIU_SECRET_KEY and QINIU_BUCKET_NAME):
    upload_to_qiniu = True
    qiniu_auth = Auth(QINIU_ACCESS_KEY, QINIU_SECRET_KEY)
    bucket = BucketManager(qiniu_auth)

flow_template = u"""
title: "{{ module_name }}"
description: |
{% if module_description %}
    <div class="well">
    {{ module_description |indent(width=4)}}
    </div>
{% endif %}

rules:
    access:
    -
        if_has_role: [student, ta, instructor]
        permissions: [view]

    grade_identifier: null

pages:

{% for page in pages %}
-
    type: Page
    id: {{ page.id }}
    content: |
        # {{ page.title|safe }}

        {{ page.content |indent(width=8)|safe }}

{% endfor %}
"""

video_template = """
<video class="video-js vjs-default-skin vjs-fluid vjs-big-play-centered" controls preload="none" data-setup='[]' playsinline>
  <source src='{{ video.url }}' type='video/mp4' />
  {% for subtitle in video.subtitles %}<track kind='captions' src='{{ subtitle.url }}' srclang='{{ subtitle.lang }}' label='{{ subtitle.lang_name}}' {% if subtitle.is_default %} default {% endif %} />
  {% endfor %}
</video>
"""

resource_template = """
<hr>

{% raw %}{% from "macros.jinja" import downloadviewpdf %}{% endraw %}

<h3>Resources</h3>
<ul>{% for asset in assets %}
  <li>{% if asset.is_pdf %}{% raw %}{{ downloadviewpdf("{% endraw %}{{asset.url}}{% raw %}", "{% endraw %}{{asset.file_name}}{% raw %}")}}{% endraw %}{% else %}
  {{ asset.asset_type }}: <a href="{{asset.url}}" target="_blank" download="{{asset.file_name}}">{{asset.name}}</a>{% endif %}</li>{% endfor %}
</ul>

"""

course_chunks_template_embed = """
-
    title: "Course: {{ course.course_name_string }}"
    id: {{ course.course_slug }}
    collapsible: True

    content: |    
        ## {{ course.course_name_string }}
        
        {% raw %}
        {% from "macros.jinja" import accordion, button, file %}
        {% endraw %}
        
        {% for flow in flows %}
        #### Module {{loop.index}}: {{ flow.name }} {% raw -%}{{ button("flow:{%- endraw -%}{{flow.flow_id}}{%- raw -%}") }}{%- endraw %}
        
        {{ flow.description }}
        
        <hr>
        
        {% endfor %}
"""

course_chunks_template_single = """
chunks:

- 
    title: "{{ course.course_name_string }}"
    id: toc
    content: |
    

{% for flow in flows %}
-
    title: "Module {{loop.index}}: {{ flow.name }}"
    id: {{course.course_slug|replace("-", "_")}}_module_{{loop.index}}
    collapsible: True

    content: |    
        {% raw %}
        {% from "macros.jinja" import accordion, button, file %}
        {% endraw %}

        #### Module {{loop.index}}: {{ flow.name }} {% raw -%}{{ button("flow:{%- endraw -%}{{flow.flow_id}}{%- raw -%}") }}{%- endraw %}

        {{ flow.description|indent(width=8) }}

        <hr>

{% endfor %}
"""


class CourseraPage(object):
    def __init__(self, id, title, content):
        self.id = id.replace("-", "_")
        self.title = title
        self.content = content


class CourseraVideoSubtitle(object):
    def __init__(self, url, lang, is_default=False):
        self.url = url
        self.lang = lang
        self.lang_name = self.get_lang_name()
        self.is_default = is_default

    def get_lang_name(self):
        maps = {'zh-CN': 'zh-hans', 'zh-TW': 'zh-hant'}
        lang = maps.get(self.lang, self.lang).lower()
        return dict(LANGUAGES).get(lang, "English")

    def __repr__(self):
        return "%s(%s)" % (self.url, self.lang_name)


class CourseraItemAsset(object):
    def __init__(self, asset_type, name, course_slug, saved_path):
        self.url = local_path_to_url(course_slug, saved_path)
        self.asset_type = asset_type
        self.name = name
        self.is_pdf = bool(self.url.lower().endswith(".pdf"))
        self.file_name = os.path.split(saved_path)[-1]


class CourseraVideo(object):
    def __init__(self, url, langs=None):
        self.url = url

        self.subtitles = []
        if langs:
            for i, lang in enumerate(langs):
                is_default = False
                if i == 0:
                    is_default = True
                self.subtitles.append(
                    CourseraVideoSubtitle(self.get_subtitle_url(lang), lang, is_default))

    def __repr__(self):
        return "%s(%s)" % (self.url, ",".join(str(sub) for sub in self.subtitles))

    def get_subtitle_url(self, lang):
        return replace_ext(self.url, ext=".%s.vtt" % lang)


def replace_ext(path, ext):
    if ext and not ext.startswith("."):
        ext = ".%s" % ext

    return os.path.splitext(path)[0] + ext


def local_path_to_url(course_slug, local_path, ext=None):
    if ext and not ext.startswith("."):
        ext = ".%s" % ext

    if ext:
        local_path = os.path.splitext(local_path)[0] + ext

    from six.moves.urllib.parse import urljoin
    if sys.platform.startswith("win"):
        assert local_path.startswith(LOCAL_PATH_PREFIX), local_path

        striped_local_path = local_path[len(LOCAL_PATH_PREFIX):]
        striped_local_path = striped_local_path.replace("\\", "/")
    else:
        assert os.path.isfile(os.path.join(os.getcwd(), local_path))
        striped_local_path = upload_resource_to_qiniu(course_slug, local_path)
    return urljoin(QINIU_BUCKET_URL_PREFIX, striped_local_path)


def convert_video_page(database, item):
    with database:
        video_assets = ItemVideoAsset.select().join(Item).where(Item.item_id == item.item_id)

    course_slug = item.lesson.module.course.course_slug

    assert len(video_assets) <= 1

    if not len(video_assets):
        return

    video_asset = video_assets[0]
    url = local_path_to_url(course_slug, video_asset.saved_path)
    sub_list = [lang.strip() for lang in video_asset.subtitles.split(",") if lang.endswith(".vtt")]
    langs = []
    for lang in ['zh-CN', 'zh-TW', 'en']:
        if lang + ".vtt" in sub_list:
            langs.append(lang)
            upload_resource_to_qiniu(course_slug, replace_ext(video_asset.saved_path, ext=".%s.vtt" % lang))

    for sub in sub_list:
        lang, _ = os.path.splitext(sub)
        if lang not in langs:
            langs.append(lang)

    video = CourseraVideo(url=url, langs=langs)

    jinja_env = jinja2.Environment()
    template = jinja_env.from_string(video_template)
    video_html = template.render(video=video)

    resource_html = ""
    item_assets = ItemAsset.select().join(Item).where(Item.item_id == item.item_id)
    if len(item_assets):
        template = jinja_env.from_string(resource_template)

        assets = []
        for item_asset in item_assets:
            if item_asset.asset.saved_path:
                asset = item_asset.asset
                assets.append(CourseraItemAsset(asset.asset_type, asset.name, course_slug, asset.saved_path))

        resource_html = template.render(assets=assets)

    output = "\n".join([video_html, resource_html])

    return output


COLON_START = re.compile(r'\n\s*:', re.M)


def avoid_colon_at_beginning(s):
    s = re.sub(COLON_START, ":", s)
    return s


def convert_normal_page(database, item):
    content = avoid_colon_at_beginning(item.content)
    soup = BeautifulSoup(content)

    try:
        course_slug = item.lesson.module.course.course_slug
    except AttributeError:
        # reference asset
        course_slug = item.course.course_slug

    # remove header tag if its content is the same with the title.
    for header_name in ["h1", "h2", "h3"]:
        header_tags = soup.find(header_name)
        if header_tags:
            try:
                header_tag_content = " ".join([str(content) for content in header_tags.contents])
            except Exception as e:
                raise e
            header_tag_content = header_tag_content.replace("\n", " ").replace("  ", " ")
            header_tag_content = header_tag_content.strip()
            if header_tag_content == item.name:
                header_tags.decompose()

    for asset_tag in soup.find_all(name="asset"):
        asset_tag.name = "a"
        asset_type = asset_tag["assettype"]
        asset_extension = asset_tag["extension"]
        asset_id = asset_tag["id"]
        asset_name = asset_tag["name"]
        with database:
            try:
                db_asset = CourseAsset.get(asset_id=asset_id)
            except CourseAsset.DoesNotExist:
                continue
        url = local_path_to_url(course_slug, db_asset.saved_path)
        asset_tag["href"] = url
        asset_tag["target"] = "_blank"

        ext = ".%s" % asset_extension.lstrip(".")
        if not asset_name.endswith(ext):
            asset_name += "(%s)" % asset_extension

        asset_tag.insert(0, NavigableString(asset_name))

    for asset_tag in soup.find_all(name="img"):
        asset_tag['class'] = asset_tag.get('class', []) + ['img-responsive']
        if not asset_tag.has_attr("assetid"):
            continue
        asset_id = asset_tag["assetid"]
        with database:
            try:
                db_asset = CourseAsset.get(asset_id=asset_id)
            except CourseAsset.DoesNotExist:
                continue
        url = local_path_to_url(course_slug, db_asset.saved_path)
        asset_tag["src"] = url

    return html.unescape(soup.decode_contents())


def generate_flow(module_slug, ordinal):
    with database:
        module = Module.get(slug=module_slug)
        items = Item.select().join(Module).where(Module.slug == module_slug)

    course_slug = module.course.course_slug
    slug = "%s_%s_%s" % (course_slug, str(ordinal), module_slug)

    flow_id = slug.replace("_", "-")
    yaml_path = "%s.yml" % flow_id
    file_name = os.path.join(os.getcwd(), yaml_path)

    pages = []
    for i, item in enumerate(items):
        if item.type_name == "lecture":
            content = convert_video_page(database, item)
        else:
            if not item.content:
                continue
            content = convert_normal_page(database, item)

        if content:
            pages.append(CourseraPage(id="%s_%s" % (item.slug, str(i+1)), title=item.name, content=content))

    jinja_env = jinja2.Environment()
    template = jinja_env.from_string(flow_template)
    output = template.render(module_name=module.name, module_description=module.description, pages=pages)

    if sys.platform.startswith("win"):
        with open(file_name, "w", encoding="utf-8") as f:
            f.write(output)

    upload_to_dropbox("/" + os.path.join(course_slug, "flows", yaml_path), output.encode())
    sys.stdout.write("%s uploaded to Dropbox.\n" % flow_id)
    return flow_id


def generate_reference_flow(course_slug, references, ordinal):
    slug = "%s_%s_resource" % (course_slug, str(ordinal))

    flow_id = slug.replace("_", "-")
    yaml_path = "%s.yml" % flow_id
    file_name = os.path.join(os.getcwd(), yaml_path)

    pages = []
    for i, item in enumerate(references):
        if not item.content:
            continue
        content = convert_normal_page(database, item)

        if content:
            pages.append(CourseraPage(id="%s_%s" % (item.slug, str(i+1)),
                                      title=item.name, content=content))

    jinja_env = jinja2.Environment()
    template = jinja_env.from_string(flow_template)
    output = template.render(module_name="Resources", pages=pages)

    if sys.platform.startswith("win"):
        with open(file_name, "w", encoding="utf-8") as f:
            f.write(output)

    upload_to_dropbox("/" + os.path.join(course_slug, "flows", yaml_path), output.encode())
    sys.stdout.write("---%s uploaded to Dropbox.---\n" % flow_id)
    return flow_id


class CourseraFlow(object):
    def __init__(self, name, flow_id, description=""):
        self.name = name
        self.flow_id = flow_id
        self.description = description


def generate_yamls(course_slug):
    with database:
        course = Course.get(course_slug=course_slug)
        modules = Module.select().join(Course).where(Course.course_slug == course_slug)
        references = Reference.select().join(Course).where(Course.course_slug == course_slug)

    flows = []
    ordinal = 0
    for i, module in enumerate(modules):
        flow_id = generate_flow(module.slug, i + 1)
        flows.append(CourseraFlow(module.name, flow_id, description=module.description))
        ordinal = i + 1

    if references:
        flow_id = generate_reference_flow(course.course_slug, references, ordinal+1)
        flows.append(CourseraFlow("Resources", flow_id))

    def generate_course_yml(template_name, yaml_path):
        jinja_env = jinja2.Environment()
        template = jinja_env.from_string(template_name)
        output = template.render(course=course, flows=flows)

        if sys.platform.startswith("win"):
            with open(yaml_path, "w", encoding="utf-8") as f:
                f.write(output)
                return

        dropbox_path = "/" + os.path.join(course_slug, yaml_path)
        upload_to_dropbox(dropbox_path, output.encode())

    # for embedded chunk
    yaml_path = "%s_course_chunks.yml" % course_slug.replace("_", "-")
    template_name = course_chunks_template_embed
    generate_course_yml(template_name, yaml_path)

    # for single course
    yaml_path = "course.yml"
    template_name = course_chunks_template_single
    generate_course_yml(template_name, yaml_path)

    sys.stdout.write("--------------Done!-----------------\n")


def upload_to_dropbox(file_name, file_content):
    if sys.platform.startswith("win"):
        return
    dropbox_token = os.environ.get("DROPBOX_ACCESS_TOKEN", "")
    if not dropbox_token:
        return

    import dropbox
    from dropbox.files import WriteMode
    dbx = dropbox.Dropbox(dropbox_token)
    return dbx.files_upload(file_content, file_name, mode=WriteMode.overwrite)


def tqdmWrapViewBar(*args, **kwargs):
    from tqdm import tqdm
    pbar = tqdm(*args, **kwargs)  # make a progressbar
    last = [0]  # last known iteration, start at 0
    def viewBar(a, b):
        pbar.total = int(b)
        pbar.update(int(a - last[0]))  # update pbar with increment
        last[0] = a  # update last known iteration
    return viewBar, pbar  # return callback, tqdmInstance


def get_bucket_course_files(course_slug):
    course_prefix = "%s/%s" % (IN_BUCKET_PREFIX, course_slug)
    ret, _, _ = bucket.list(bucket=QINIU_BUCKET_NAME, prefix=course_prefix)
    return ret['items']


def upload_resource_to_qiniu(course_slug, file_path):
    if not qiniu_auth or not upload_to_qiniu:
        return

    _, ext = os.path.splitext(file_path)
    if ext.lower() in [".jpg", ".png", ".gif"]:
        basewidth = 1024

        from PIL import Image
        img = Image.open(file_path)
        img_width, img_height = img.size

        if img_width > basewidth:
            wpercent = (basewidth / float(img_width))
            hsize = int((float(img_height) * float(wpercent)))
            img = img.resize((basewidth, hsize), Image.ANTIALIAS)
            img.save(file_path)

    qiniu_file_path = os.path.join(IN_BUCKET_PREFIX, file_path)

    file_etag = etag(file_path)
    ret, _ = bucket.stat(QINIU_BUCKET_NAME, qiniu_file_path)

    # Check if the file exists / changed, if not, upload or update.
    if ret and "hash" in ret:
        if file_etag == ret["hash"]:
            sys.stdout.write("File with hash '%s' already exist.\n" % file_etag)
            return qiniu_file_path

    bucket_course_files = get_bucket_course_files(course_slug)
    for item in bucket_course_files:
        if item['hash'] == file_etag:
            sys.stdout.write(
                "File with hash '%s' already exist (with another name).\n"
                % file_etag)
            return item['key']

    sys.stdout.write(
        "File with hash '%s' changed, will be overwritten.\n" % file_etag)

    size = os.stat(file_path).st_size / 1024 / 1024
    sys.stdout.write(
        "Uploading file with hash %s (size: %.1fM)\n" % (file_etag, size))
    token = qiniu_auth.upload_token(QINIU_BUCKET_NAME, qiniu_file_path, 3600)

    cbk, pbar = tqdmWrapViewBar(ascii=True, unit='b', unit_scale=True)
    ret, _ = put_file(token, qiniu_file_path, file_path, progress_handler=cbk)

    pbar.close()
    return ret['key']


def remove_duplicate_files(course_slug):
    course_files = get_bucket_course_files(course_slug)
    exist_hashes = []
    n_deleted_file = 0
    for course_file in course_files:
        if course_file["hash"] in exist_hashes:
            bucket.delete(QINIU_BUCKET_NAME, course_file["key"])
            n_deleted_file += 1
        else:
            exist_hashes.append(course_file["hash"])

    sys.stdout.write(
        "---%d duplicated files where deleted.---\n" % n_deleted_file)


def remove_specific_files(course_slug, extension=".pdf"):
    course_files = get_bucket_course_files(course_slug)
    if not course_files:
        return

    n_deleted_file = 0
    for course_file in course_files:
        if course_file["key"].lower().endswith(extension.lower()):
            bucket.delete(QINIU_BUCKET_NAME, course_file["key"])
            n_deleted_file += 1

    sys.stdout.write(
        "---%d duplicated files where deleted.---\n" % n_deleted_file)


def main():
    if os.path.isfile(DB_PATH):
        with open(DB_PATH, 'rb') as f:
            data = f.read()
            upload_to_dropbox("/course_%s.db" % datetime.datetime.now().strftime("%Y-%m-%d-%H-%M"), data)

    try:
        with database:
            courses = Course.select()

        course_slug_list = [c.course_slug for c in courses]
        for course_slug in course_slug_list:
            # remove_duplicate_files(course_slug)
            # remove_specific_files(course_slug)
            # remove_specific_files(course_slug, extension=".jpg")
            # remove_specific_files(course_slug, extension=".png")
            generate_yamls(course_slug)

    except OperationalError as e:
        if "no such table" in str(e):
            sys.stdout.write("Warning: No Course has been downloaded.")
            sys.exit(1)
        else:
            raise e


if __name__ == "__main__":
    main()
