#!/usr/bin/env python3
"""
groma2ojs - convert the HTML exported by InDesign for a whole GROMA volume into
one clean folder per article, ready to be uploaded to OJS.

For every article it produces:
    NN-surname-title-words/
        article.html     galley (HTML fragment wrapped in a minimal document)
        metadata.json    title, authors (email, affiliation), abstract, keywords...
        references.txt   one reference per line (paste into the OJS "References" box)
        images/          only the images of that article
and, at volume level:
    volume-metadata.json / volume-metadata.csv / report.md

Python rewrite of https://github.com/lad-sapienza/tranformHTML (same output
conventions: div#newContainer.article-text, a.ftpopover, p.footnote-foot,
ul.bibliography, ul.gallery with fancybox links) adapted to the newer InDesign
export (footnotes in section._idFootnotes, bold/italic encoded as CharOverride-N
classes that must be resolved through the exported CSS, figures outside the
article container, ...).

Requires: beautifulsoup4, lxml  (+ Pillow for the image routine: resize + WebP)
Usage:    python3 groma2ojs.py "GROMA 10 2025 text.html" -o out/groma10 [--only 1,3] [--list]
"""
from __future__ import annotations

import argparse
import csv
import difflib
import html as htmllib
import json
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import unquote

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

NBSP = " "

# ----------------------------------------------------------------------------
# CSS: resolve what a CharOverride-N / Strong / Emphasis class really means
# ----------------------------------------------------------------------------
BOLD_NAMES = {"Strong", "Bold", "Bold-Italic"}
ITALIC_NAMES = {"Emphasis", "Italic", "Bold-Italic"}


class Styles:
    """Tiny CSS reader: only .class rules, only the properties we care about."""

    def __init__(self, css_path: Path | None, strict_css: bool = False):
        self.rules: list[tuple[str, dict]] = []
        self.strict = strict_css
        self.cache: dict[tuple, dict] = {}
        if css_path and css_path.exists():
            text = re.sub(r"/\*.*?\*/", "", css_path.read_text(encoding="utf-8"), flags=re.S)
            for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
                props = {}
                for decl in body.split(";"):
                    if ":" in decl:
                        k, v = decl.split(":", 1)
                        props[k.strip().lower()] = v.strip().lower()
                for s in sel.split(","):
                    m = re.fullmatch(r"\s*[a-zA-Z0-9]*\.([\w-]+)\s*", s)
                    if m:
                        self.rules.append((m.group(1), props))

    def resolve(self, classes) -> dict:
        key = tuple(classes)
        if key in self.cache:
            return self.cache[key]
        cs = set(classes)
        eff: dict = {}
        for cls, props in self.rules:          # later rules win, as in the browser
            if cls in cs:
                eff.update(props)
        weight = eff.get("font-weight", "")
        flags = {
            "bold": weight == "bold" or (weight.isdigit() and int(weight) >= 600),
            "italic": eff.get("font-style", "") in ("italic", "oblique"),
            "sup": eff.get("vertical-align", "") == "super",
            "sub": eff.get("vertical-align", "") == "sub",
        }
        if not self.strict:
            # A character style *named* Strong/Emphasis is the editor's intent,
            # even if a local "CharOverride" resets the weight in the CSS.
            if cs & BOLD_NAMES:
                flags["bold"] = True
            if cs & ITALIC_NAMES:
                flags["italic"] = True
        self.cache[key] = flags
        return flags


def convert_inline(root: Tag, styles: Styles) -> None:
    """span.<anything> -> strong / em / sup / sub (or unwrapped)."""
    soup_for_new = root
    while soup_for_new.parent is not None:
        soup_for_new = soup_for_new.parent
    for sp in list(root.find_all("span")):
        flags = styles.resolve(sp.get("class", []))
        tags = [t for t, on in (("strong", flags["bold"]), ("em", flags["italic"]),
                                ("sup", flags["sup"]), ("sub", flags["sub"])) if on]
        sp.attrs = {}
        if not tags:
            sp.unwrap()
            continue
        sp.name = tags[0]
        for extra in tags[1:]:
            wrapper = soup_for_new.new_tag(extra)
            wrapper.extend(list(sp.contents))
            sp.append(wrapper)


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------
@dataclass
class Author:
    name: str
    email: str = ""
    affiliation: str = ""
    corresponding: bool = False


@dataclass
class Article:
    index: int
    section: str
    title: str = ""
    authors: list = field(default_factory=list)
    abstract: str = ""
    keywords: list = field(default_factory=list)
    references: list = field(default_factory=list)
    folder: str = ""
    images: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def classes(el: Tag) -> list:
    return el.get("class", []) if isinstance(el, Tag) else []


def text_of(el: Tag) -> str:
    """Plain text of an element: <br> becomes a space, inline tags add nothing."""
    clone = BeautifulSoup(str(el), "lxml")
    for br in clone.find_all("br"):
        br.replace_with(" ")
    return norm(clone.get_text(""))


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(NBSP, " ")).strip()


def slugify(text: str, max_words: int = 6) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:max_words])


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def inner_html(el: Tag) -> str:
    return "".join(str(c) for c in el.contents).strip(" \n\t" + NBSP)


EMAIL_RE = re.compile(r"[\w.+-]+\s*@\s*[\w-]+(?:\.[\w-]+)+")


def parse_author_info(text: str) -> tuple[str, str, bool]:
    """'(corresponding author) a.b @uni.it – University of X, Dept Y' -> email, affiliation, corresponding"""
    text = norm(text)
    corresponding = bool(re.search(r"corresponding", text, re.I))
    text = re.sub(r"\(?\s*corresponding author\s*\)?", "", text, flags=re.I)
    email = ""
    m = EMAIL_RE.search(text)
    if m:
        email = re.sub(r"\s+", "", m.group(0))
        text = text[:m.start()] + text[m.end():]
    affiliation = text.strip(" \t–—-,;:")
    return email, affiliation, corresponding


FIG_LABEL = r"(?:Figures?|Figs?\.?|Plates?|Pls?\.?)"
CAPTION_RE = re.compile(r"^\s*(" + FIG_LABEL + r")[\s ]*(\d+[a-z]?)(?![A-Za-z0-9])", re.I)
REF_RE = re.compile(
    r"\b(" + FIG_LABEL + r")([\s ]*)(\d+[a-z]?(?:\s*(?:[–—\-]|,|;|and|&)\s*\d+[a-z]?)*)(?![A-Za-z0-9])"
)


def fig_key(label: str, num: str) -> str:
    return ("plate" if label.lower().startswith("pl") else "figure") + "_" + num.lower()


# ----------------------------------------------------------------------------
# Volume segmentation
# ----------------------------------------------------------------------------
@dataclass
class RawArticle:
    index: int
    section: str
    container: Tag
    extras: list = field(default_factory=list)


def split_volume(soup: BeautifulSoup) -> list[RawArticle]:
    arts: list[RawArticle] = []
    section = ""
    current: RawArticle | None = None
    for el in soup.body.find_all(recursive=False):
        if not isinstance(el, Tag):
            continue
        if el.find(class_="Section-Header-TOC"):          # table of contents
            current = None
            continue
        sec = el.find("h1", class_="Section-Header", recursive=False)
        if sec:
            section = norm(sec.get_text())
            current = None
            continue
        if el.find("h1", class_="Chapter-heading", recursive=False):
            current = RawArticle(len(arts) + 1, section, el)
            arts.append(current)
        elif current is not None:
            current.extras.append(el)
    return arts


# ----------------------------------------------------------------------------
# Metadata
# ----------------------------------------------------------------------------
def extract_metadata(raw: RawArticle, art: Article) -> None:
    c = raw.container
    h1 = c.find("h1", class_="Chapter-heading")
    art.title = text_of(h1) if h1 else ""

    current: Author | None = None
    for p in c.find_all("p"):
        cl = classes(p)
        if "Author" in cl:
            name = text_of(p)
            if " and " in name or "," in name:
                art.warnings.append(f"Author line looks like several people: “{name}”")
            current = Author(name)
            art.authors.append(current)
        elif any(x in cl for x in ("Author-info", "Author-info-multi")):
            email, aff, corr = parse_author_info(text_of(p))
            if current is None:
                art.warnings.append("Author-info found before any Author paragraph")
                continue
            current.email = current.email or email
            current.affiliation = (current.affiliation + "; " + aff) if current.affiliation and aff else (current.affiliation or aff)
            current.corresponding = current.corresponding or corr
    for a in art.authors:
        if not a.email:
            art.warnings.append(f"No email found for author “{a.name}”")

    paras = [text_of(p) for p in c.find_all("p", class_="Abstract")]
    art.abstract = "\n\n".join(x for x in paras if x)
    kw = " ".join(text_of(p) for p in c.find_all("p", class_="Key-Words"))
    art.keywords = [k.strip(" .") for k in re.split(r"[;,]", kw) if k.strip(" .")]
    if art.section.lower() == "articles":
        if not art.abstract:
            art.warnings.append("No abstract found")
        if not art.keywords:
            art.warnings.append("No keywords found")


# ----------------------------------------------------------------------------
# Body conversion
# ----------------------------------------------------------------------------
META_CLASSES = {"Author", "Author-info", "Author-info-multi", "Key-Words", "Abstract",
                "Author-Not-for-RH", "Abstract-heading", "Chapter-heading"}


def collect_footnotes(group: list[Tag], styles: Styles, art: Article) -> dict:
    notes = {}
    for root in group:
        for li in root.select("li._idFootnote"):
            p = li.find("p") or li
            for sp in p.select("span.FootnoteNo"):
                sp.decompose()
            convert_inline(p, styles)
            notes[li.get("id", "")] = inner_html(p)
        for sec in root.select("section._idFootnotes"):
            sec.decompose()
    return notes


def match_images_to_captions(imgs: list[Tag], caps: list[Tag], art: Article):
    """Pair each <img> with its caption using the alt text (falls back to order)."""
    cap_txt = [text_of(c) for c in caps]
    alt_txt = [norm(htmllib.unescape(i.get("alt", ""))).strip('"“”') for i in imgs]
    pairs: dict[int, int] = {}
    used_c: set = set()
    for i, alt in enumerate(alt_txt):                     # several images may share one caption
        best, best_j = 0.0, None
        for j, ct in enumerate(cap_txt):
            if alt:
                r = difflib.SequenceMatcher(None, alt[:120].lower(), ct[:120].lower()).ratio()
                if r > best:
                    best, best_j = r, j
        if best_j is not None and best >= 0.75:
            pairs[i] = best_j
            used_c.add(best_j)
    free_i = [i for i in range(len(imgs)) if i not in pairs]
    free_c = [j for j in range(len(caps)) if j not in used_c]
    for i, j in zip(free_i, free_c):                      # order-based fallback
        pairs[i] = j
        used_c.add(j)
        art.warnings.append(f"Image {Path(imgs[i]['src']).name} paired to caption “{cap_txt[j][:50]}…” by order (alt text did not match)")
    for i in range(len(imgs)):
        if i not in pairs:
            art.warnings.append(f"Image {Path(imgs[i]['src']).name} has no caption")
    for j in range(len(caps)):
        if j not in used_c:
            art.warnings.append(f"Caption without image: “{cap_txt[j][:70]}…”")
    return pairs


def clean_table(table: Tag, soup: BeautifulSoup) -> Tag:
    for cg in table.find_all(["colgroup", "col"]):
        cg.decompose()
    for cell in table.find_all(["td", "th"]):
        ps = cell.find_all("p")
        for k, p in enumerate(ps):
            if k < len(ps) - 1:
                p.insert_after(soup.new_tag("br"))
            p.unwrap()
        keep = {k: v for k, v in cell.attrs.items() if k in ("colspan", "rowspan", "scope")}
        cell.attrs = keep
    for t in table.find_all(["tr", "thead", "tbody", "tfoot"]):
        t.attrs = {}
    table.attrs = {}
    return table


def sanitize(root: Tag) -> None:
    for el in root.find_all(True):
        if el.name == "a":
            href = el.get("href", "")
            if href.startswith(("http://", "https://", "mailto:")):
                el.attrs = {"href": href}
            elif "ftpopover" in classes(el):
                pass
            elif not el.get_text(strip=True):
                el.decompose()
            else:
                el.unwrap()
        elif el.name in ("td", "th", "table", "tr", "thead", "tbody"):
            continue
        else:
            el.attrs = {k: v for k, v in el.attrs.items() if k == "data-bib"}


def remove_empty(root: Tag) -> None:
    changed = True
    while changed:
        changed = False
        for el in root.find_all(["p", "span", "div", "strong", "em", "sup", "sub", "li"]):
            if not el.get_text(strip=True).replace(NBSP, "") and not el.find(["img", "table"]):
                el.decompose()
                changed = True


def build_article(raw: RawArticle, art: Article, soup: BeautifulSoup, styles: Styles,
                  src_dir: Path, art_dir: Path, opts) -> str:
    c = raw.container
    group = [c] + raw.extras

    # 1. images & captions (they live in the siblings after the article container)
    imgs = [i for g in group for i in g.find_all("img")]
    caps = [p for g in group for p in g.find_all("p", class_="Caption")]
    for cap in caps:
        convert_inline(cap, styles)
        sanitize(cap)
    table_caps = [c for c in caps if re.match(r"\s*Tables?\b", text_of(c))]
    caps = [c for c in caps if c not in table_caps]
    merged: list[Tag] = []
    for cap in caps:                                   # caption split over two text frames
        if merged and not CAPTION_RE.match(text_of(cap)):
            art.warnings.append(f"Caption continuation merged into the previous caption: “{text_of(cap)[:50]}…”")
            merged[-1].append(" ")
            merged[-1].extend(list(cap.contents))
        else:
            merged.append(cap)
    caps = merged
    pairs = match_images_to_captions(imgs, caps, art)
    tables = [t for g in raw.extras for t in g.find_all("table")]

    # 2. footnotes text (detached) then drop footnote section
    notes = collect_footnotes([c], styles, art)

    # 3. other text in the extras we are NOT using -> report, don't lose silently
    for g in raw.extras:
        for p in g.find_all(["p", "h1", "h2", "h3"]):
            if "Caption" in classes(p) or p.find_parent("table"):
                continue
            t = norm(p.get_text(" "))
            names = {a.name for a in art.authors}
            if t and t not in names and t.replace(" and ", ", ").replace(" & ", ", ").split(", ")[0] not in names:
                art.warnings.append(f"Ignored text outside the article frame: “{t[:60]}”")

    # 4. inline styles on the main container
    for node in c.select(".Abstract-heading, .Abstract, .Key-Words, .Author, .Author-info, .Author-info-multi, .Author-Not-for-RH, h1.Chapter-heading, hr"):
        node.decompose()
    convert_inline(c, styles)

    # 5. new container, block by block
    out = BeautifulSoup('<div class="article-text" id="newContainer"></div>', "lxml")
    root = out.find("div")

    def emit(el: Tag):
        cl = classes(el)
        name = el.name
        if name == "div":
            for ch in list(el.children):
                if isinstance(ch, Tag):
                    emit(ch)
            return
        if name in ("h2", "h3", "h4", "h5", "h6"):
            for tag in el.find_all(["strong"] + (["em"] if name == "h3" else [])):
                tag.unwrap()
            root.append(el.extract())
        elif name == "h1":
            el.name = "h2"
            art.warnings.append(f"Unexpected h1 turned into h2: “{norm(el.get_text())[:50]}”")
            root.append(el.extract())
        elif name == "p":
            if "bibliography" in cl:
                el["data-bib"] = "1"
            elif "Caption" in cl:
                return
            root.append(el.extract())
        elif name in ("ul", "ol", "table", "blockquote"):
            if name == "table":
                clean_table(el, out)
            root.append(el.extract())
        elif name in ("section", "hr"):
            return
        else:
            art.warnings.append(f"Unhandled element <{name} class='{' '.join(cl)}'> dropped")

    for ch in list(c.children):
        if isinstance(ch, Tag):
            emit(ch)

    # unknown paragraph styles worth knowing about
    known = {"Normal", "Heading-1", "Heading-2", "bibliography", "Footnotetxt", "bulleted", "Unused_numbered-"}
    for el in root.find_all(["p", "li"]):
        odd = [x for x in classes(el) if x not in known and not x.startswith(("ParaOverride", "_id"))]
        if odd:
            art.warnings.append(f"Unrecognised paragraph style(s) {odd} left as plain <{el.name}>")

    # 6. footnote references
    note_list: list[tuple[str, str]] = []
    for a in list(root.find_all("a", class_="_idFootnoteLink")):
        num = norm(a.get_text())
        target = a.get("href", "").split("#")[-1]
        note_html = notes.get(target)
        wrapper = a
        while (wrapper.parent is not root and wrapper.parent.name in ("span", "sup", "em", "strong", "sub")
               and wrapper.parent.get_text(strip=True) == wrapper.get_text(strip=True)):
            wrapper = wrapper.parent
        if note_html is None:
            art.warnings.append(f"Footnote {num} (#{target}) referenced but text not found")
            wrapper.replace_with(NavigableString(f"[{num}]"))
            continue
        new = out.new_tag("a", attrs={"href": "javascript:void(0);", "class": "ftpopover",
                                      "id": f"bd-note-{num}", "data-content": note_html})
        new.string = num
        wrapper.replace_with(new)
        note_list.append((num, note_html))
    if len(note_list) != len(notes):
        art.warnings.append(f"{len(note_list)} footnote references in text vs {len(notes)} footnotes")

    sanitize(root)

    # 7. bibliography: consecutive p[data-bib] -> ul.bibliography
    ul = None
    for p in list(root.find_all("p", attrs={"data-bib": True})):
        if ul is None or p.find_previous_sibling() is not ul:
            ul = out.new_tag("ul", attrs={"class": "bibliography"})
            p.insert_before(ul)
        li = out.new_tag("li")
        li.extend(list(p.contents))
        ul.append(li)
        art.references.append(text_of(li))
        p.decompose()
    if not art.references and art.section.lower() == "articles":
        art.warnings.append("No bibliography found")

    # 8. images: pick the folder of originals, copy / convert and build the gallery
    art_images = art_dir / "images"
    gallery_items = []
    prep = []
    for i, img in enumerate(imgs):
        cap_el = caps[pairs[i]] if i in pairs else None
        cap_text = text_of(cap_el) if cap_el is not None else norm(htmllib.unescape(img.get("alt", ""))).strip('"“”')
        cap_html = inner_html(cap_el) if cap_el is not None else htmllib.escape(cap_text)
        m = CAPTION_RE.match(cap_text)
        prep.append({"img": img, "src": unquote(img.get("src", "")), "cap_text": cap_text, "cap_html": cap_html,
                     "key": fig_key(m.group(1), m.group(2)) if m else "", "num": m.group(2).lower() if m else None})
    group = pick_group(opts.images_index, [(Path(x["src"]).stem, x["num"], src_dir / x["src"]) for x in prep], art.warnings,
                       [a.name.split()[-1] for a in art.authors[:1] if a.name.split()]) if opts.images_index else None
    taken_orig: set = set()
    for x in prep:
        src_file = src_dir / x["src"]
        if group:
            orig = group.find(Path(x["src"]).stem, x["num"], art.warnings)
            if orig is None:
                orig = visual_match(src_dir / x["src"], group.files, taken_orig)
            if orig:
                taken_orig.add(orig)
                src_file = orig
            else:
                art.warnings.append(f"No original for {Path(x['src']).name} in “{group.path.name}”: InDesign export used")
        if not src_file.exists():
            art.warnings.append(f"Image file missing: {x['src']}")
            continue
        gallery_items.append({"key": x["key"], "src_file": src_file, "orig": Path(x["src"]).name,
                              "caption": x["cap_text"], "caption_html": x["cap_html"]})

    # unique keys; items without a number get figure_x1...
    seen, auto = set(), 0
    for g in gallery_items:
        if not g["key"]:
            auto += 1
            g["key"] = f"figure_x{auto}"
        base, n = g["key"], 2
        while g["key"] in seen:
            g["key"] = f"{base}_{n}"
            n += 1
            art.warnings.append(f"Several images share caption “{base}” → ids {base}, {g['key']} (link in text goes to the first)")
        seen.add(g["key"])

    def sort_key(g):
        m = re.match(r"(?:figure|plate)_(\d+)([a-z]?)", g["key"])
        return (0, int(m.group(1)), m.group(2)) if m else (1, 0, g["key"])
    gallery_items.sort(key=sort_key)

    # consistent file names (<surname>-fig-06.webp) unless --keep-image-names; then convert/copy the files
    surname = slugify(art.authors[0].name.split()[-1], 1) if art.authors else "article"
    forced_ext = {"webp": ".webp", "jpeg": ".jpg"}.get(opts.format)
    used_names: set = set()
    for g in gallery_items:
        if opts.keep_image_names:
            stem = Path(safe_filename(g["orig"])).stem
        else:
            m = re.match(r"(figure|plate)_(\d+)([a-z]?)(?:_(\d+))?$", g["key"])
            if m:
                stem = (f"{surname}-{'fig' if m.group(1) == 'figure' else 'plate'}-{int(m.group(2)):02d}{m.group(3)}"
                        + (f"-{m.group(4)}" if m.group(4) else ""))
            else:
                stem = f"{surname}-{g['key'].replace('_', '-')}"
        ext = forced_ext or Path(g["src_file"]).suffix.lower()
        name, n = stem + ext, 2
        while name in used_names:
            name = f"{stem}-{n}{ext}"
            n += 1
        used_names.add(name)
        g["file"] = name
        g["href"] = g["thumb"] = name
        if not opts.dry:
            art_images.mkdir(parents=True, exist_ok=True)
            process_image(g["src_file"], art_images / name, opts)
            if opts.inline_images:
                blob, how = fit_full_image(g["src_file"], opts)
                if how:
                    art.warnings.append(f"{name}: reduced to {how} so that its data URI stays under {opts.inline_max_chars:,} characters")
                g["href"] = data_uri(blob, "image/webp")
                g["thumb"] = data_uri(thumb_bytes(art_images / name), "image/webp")
        art.images.append(name)
    by_key = {g["key"]: g for g in gallery_items}

    # 9. in-text Figure/Plate references -> links
    stats = {"linked": 0, "unresolved": set()}

    def link_html(text: str, g: dict) -> str:
        cap = htmllib.escape(g["caption"], quote=True)
        if opts.inline_images:
            # A data URI repeated at every mention would bloat the file: re-use the gallery item. With fancybox
            # loaded it opens that item's lightbox, otherwise the link jumps down to the gallery.
            k = g["key"]
            js = f"if(window.jQuery&&jQuery.fancybox){{jQuery('#{k} a.fancybox').trigger('click');return false;}}"
            return f'<a href="#{k}" onclick="{js}" title="{cap}">{htmllib.escape(text)}</a>'
        return (f'<a href="{htmllib.escape(g["file"], quote=True)}" data-fancybox="gallery" rel="gallery" '
                f'data-caption="{cap}" title="{cap}">{htmllib.escape(text)}</a>')

    def repl(m):
        label, sp, nums = m.group(1), m.group(2), m.group(3)
        toks = list(re.finditer(r"\d+[a-z]?", nums))
        def lookup(tok):
            g = by_key.get(fig_key(label, tok))
            if g is None and tok[-1].isalpha():
                g = by_key.get(fig_key(label, tok[:-1]))
            return g
        if len(toks) == 1:
            g = lookup(toks[0].group(0))
            if not g:
                stats["unresolved"].add(m.group(0))
                return htmllib.escape(m.group(0))
            stats["linked"] += 1
            return link_html(m.group(0), g)
        res, pos = [htmllib.escape(label + sp)], 0
        for t in toks:
            res.append(htmllib.escape(nums[pos:t.start()]))
            g = lookup(t.group(0))
            if g:
                stats["linked"] += 1
                res.append(link_html(t.group(0), g))
            else:
                stats["unresolved"].add(f"{label} {t.group(0)}")
                res.append(htmllib.escape(t.group(0)))
            pos = t.end()
        res.append(htmllib.escape(nums[pos:]))
        return "".join(res)

    def link_figures(scope: Tag):
        for t in list(scope.find_all(string=True)):
            if isinstance(t, Comment) or t.find_parent("a") or not REF_RE.search(t):
                continue
            # split text around matches, escape the unmatched parts ourselves
            s, pos, parts = str(t), 0, []
            for m in REF_RE.finditer(s):
                parts.append(htmllib.escape(s[pos:m.start()], quote=False))
                parts.append(repl(m))
                pos = m.end()
            parts.append(htmllib.escape(s[pos:], quote=False))
            new = BeautifulSoup("".join(parts), "html.parser")
            t.replace_with(*list(new.contents))

    link_figures(root)
    if stats["unresolved"]:
        art.warnings.append("Figure/Plate mentions with no matching image (maybe citations of other works): "
                            + ", ".join(sorted(stats["unresolved"])))

    # 10. tables: put each after the first paragraph that mentions it, else before the bibliography
    for k, tb in enumerate(tables, 1):
        convert_inline(tb, styles)
        tb = clean_table(tb.extract(), out)
        sanitize(tb)
        if k <= len(table_caps):
            cap_tag = out.new_tag("caption")
            cap_tag.extend(list(table_caps[k - 1].contents))
            tb.insert(0, cap_tag)
        else:
            art.warnings.append(f"Table {k} has no caption")
        anchor = None
        for p in root.find_all("p"):
            if re.search(rf"\bTable\s*{k}\b", p.get_text()):
                anchor = p
                break
        if anchor is None:
            anchor = root.find("ul", class_="bibliography")
            anchor = anchor.find_previous_sibling() if anchor else root.find_all(recursive=False)[-1]
            art.warnings.append(f"Table {k}: no paragraph mentions it, placed before the references")
        anchor.insert_after(tb)

    remove_empty(root)

    # 11. footnotes at the end + gallery
    if note_list:
        root.append(out.new_tag("hr"))
        div = out.new_tag("div", attrs={"class": "footnotes"})
        root.append(div)
        for num, text in sorted(note_list, key=lambda x: int(x[0]) if x[0].isdigit() else 0):
            frag = BeautifulSoup(
                f'<p class="footnote-foot" id="note-{num}"><a id="ft-note-{num}" href="#bd-note-{num}">{num}</a></p>'
                f'<div class="footnote-text" id="note-text-{num}">{text}</div>', "html.parser")
            div.extend(list(frag.contents))
    if gallery_items:
        root.append(out.new_tag("hr"))
        parts = ['<div class="gallery-container"><ul class="gallery">']
        for g in gallery_items:
            cap = htmllib.escape(g["caption"], quote=True)
            parts.append(
                f'<li id="{g["key"]}" data-id="{htmllib.escape(g["file"], quote=True)}">'
                f'<a class="fancybox" href="{htmllib.escape(g["href"], quote=True)}" data-caption="{cap}" '
                f'title="{cap}" rel="gallery" data-fancybox="gallery"><img src="{htmllib.escape(g["thumb"], quote=True)}" alt="{cap}" /></a>'
                f'<div class="caption">{g["caption_html"]}</div></li>')
        parts.append("</ul></div>")
        root.append(BeautifulSoup("".join(parts), "html.parser"))

    for el in root.find_all(attrs={"data-bib": True}):
        del el["data-bib"]

    body = "\n".join(str(ch) for ch in root.children if not (isinstance(ch, NavigableString) and not ch.strip()))

    title = htmllib.escape(art.title)
    return (f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8" />\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1.0" />\n<title>{title}</title>\n</head>\n'
            f'<body>\n<div class="article-text" id="newContainer">\n{body}\n</div>\n</body>\n</html>\n')


IMG_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".gif", ".bmp"}
# folders that contain InDesign exports / drafts, never originals
DEFAULT_EXCLUDE = ["GROMA * HTML", "*-web-resources", "BOZZE*", "drafts*"]


def squash(stem: str) -> str:
    return re.sub(r"[^a-z0-9]", "", stem.lower())


FIGNUM_RE = re.compile(r"fig(?:ure|ura|s)?\.?[\s_\-]*(\d+[a-z]?)(?![a-z0-9])", re.I)


def num_from_name(stem: str):
    """'Fig. 10 ortofoto' -> '10', 'Figure 11a' -> '11a', '3' -> '3', 'key image' -> None"""
    m = FIGNUM_RE.search(stem)
    if m:
        return m.group(1).lower()
    m = re.fullmatch(r"\s*(\d+[a-z]?)\s*", stem)
    return m.group(1).lower() if m else None


_SIG_CACHE: dict = {}


def image_sig(path: Path, n: int = 24):
    """Tiny normalised grayscale thumbnail used to recognise the same picture under a different name."""
    import math
    key = str(path)
    if key not in _SIG_CACHE:
        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = None
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                im.draft("L", (n * 8, n * 8))
                d = list(im.convert("L").resize((n, n), Image.BILINEAR).getdata())
        except Exception:
            _SIG_CACHE[key] = None
            return None
        m = sum(d) / len(d)
        v = [x - m for x in d]
        norm_ = math.sqrt(sum(x * x for x in v)) or 1.0
        _SIG_CACHE[key] = [x / norm_ for x in v]
    return _SIG_CACHE[key]


def visual_match(export: Path, files: list, taken: set | None = None):
    """Best file by picture content (correlation >= 0.6, clearly better than the runner-up)."""
    a = image_sig(export)
    if a is None:
        return None
    res = []
    for f in files:
        if taken and f in taken:
            continue
        b = image_sig(f)
        if b is not None:
            res.append((sum(x * y for x, y in zip(a, b)), f))
    res.sort(key=lambda t: t[0], reverse=True)
    if res and res[0][0] >= 0.6 and (len(res) == 1 or res[0][0] - res[1][0] >= 0.2):
        return res[0][1]
    return None


class OrigGroup:
    """Images that live in one folder (= the figures of one article)."""

    def __init__(self, path: Path, files: list):
        self.path = path
        self.stems: dict = {}
        self.nums: dict = {}
        for f in files:
            self.stems.setdefault(squash(f.stem), []).append(f)
            n = num_from_name(f.stem)
            if n:
                self.nums.setdefault(n, []).append(f)
        self.files = list(files)
        self.mtime = max(f.stat().st_mtime for f in files)

    @staticmethod
    def _best(cands: list, notes, label):
        if len(cands) > 1:
            cands = sorted(cands, key=lambda f: (len(f.stem), -f.stat().st_size))
            if notes is not None:
                notes.append(f"{label}: {len(cands)} candidate files, used “{cands[0].name}” (others: "
                             + ", ".join(f.name for f in cands[1:]) + ")")
        return cands[0]

    def find(self, stem: str, num, notes=None):
        c = self.stems.get(squash(stem))
        if c:
            return self._best(c, notes, stem)
        if num and num in self.nums:
            return self._best(self.nums[num], notes, stem)
        return None

    def score(self, items) -> int:
        return sum(1 for stem, num, _ in items if squash(stem) in self.stems or (num and num in self.nums))

    def visual_score(self, items) -> int:
        return sum(1 for _, _, ex in items if visual_match(ex, self.files))


def index_images(dirs, exclude=DEFAULT_EXCLUDE) -> list:
    import fnmatch
    by_dir: dict = {}
    for d in dirs:
        d = Path(d)
        for f in sorted(d.rglob("*")):
            if f.suffix.lower() not in IMG_EXT or f.name.startswith("."):
                continue
            if any(fnmatch.fnmatch(x, pat) for x in f.relative_to(d).parts[:-1] for pat in exclude):
                continue
            by_dir.setdefault(f.parent, []).append(f)
    return [OrigGroup(k, v) for k, v in by_dir.items()]


def pick_group(groups: list, items: list, notes: list, surnames=()):
    """Folder of originals for this article.
    1. folders whose path contains an author's surname, best figure match wins;
    2. otherwise only folders whose file *names* equal the export names (no guessing by number).
    Ties -> most recently modified, and the alternatives are reported."""
    if not items:
        return None
    need = max(1, (len(items) + 1) // 2)
    sn = [squash(x) for x in surnames if len(squash(x)) >= 4]
    cands = [g for g in groups if any(x in squash(str(g.path)) for x in sn)]
    if cands:
        score = lambda g: g.score(items)
        if max(score(g) for g in cands) < need:          # names say nothing: compare the pictures themselves
            score = lambda g: g.visual_score(items)
            notes.append("Originals recognised by picture content (file names do not correspond)")
    else:
        cands = groups
        score = lambda g: sum(1 for stem, _, _ in items if squash(stem) in g.stems)
    scored = sorted(((score(g), g.mtime, g) for g in cands), key=lambda t: (t[0], t[1]), reverse=True)
    if not scored or scored[0][0] < need:
        notes.append(f"No folder of originals found for these {len(items)} images: InDesign export used")
        return None
    best = scored[0]
    where = lambda g: f"{g.path.parent.name}/{g.path.name}"
    rivals = [where(g) for sc, _, g in scored[1:] if sc == best[0]]
    if rivals:
        notes.append(f"Originals taken from “{where(best[2])}” (most recent); equally good: " + ", ".join(rivals))
    return best[2]


def render_webp(src: Path, side: int, quality: int) -> bytes:
    import io
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if side and max(im.size) > side:
            k = side / max(im.size)
            im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        im = im.convert("RGBA" if has_alpha else "RGB")
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=quality, method=6)
    return buf.getvalue()


def fit_full_image(src: Path, opts):
    """Full-size WebP whose base64 form stays under opts.inline_max_chars (a very long attribute value breaks
    OJS's HTML processing). Lower the quality first, then the size. Returns (bytes, description or '')."""
    limit = int(opts.inline_max_chars * 3 / 4) - 64          # base64 inflates by 4/3
    side, quality = opts.max_side, opts.quality
    first = (side, quality)
    while True:
        blob = render_webp(src, side, quality)
        if len(blob) <= limit or (side and side < 700):
            return blob, ("" if (side, quality) == first else f"{side}px q{quality}")
        if quality > 55:
            quality -= 7
        else:
            side = int((side or 2000) * 0.85)
            quality = max(55, opts.quality - 10)


def data_uri(src, mime=None) -> str:
    """base64 data URI of a file path or of raw bytes."""
    import base64
    import mimetypes
    if isinstance(src, (bytes, bytearray)):
        data = bytes(src)
    else:
        data = Path(src).read_bytes()
        mime = mime or mimetypes.guess_type(str(src))[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def thumb_bytes(path: Path, side: int = 480, quality: int = 70) -> bytes:
    import io
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGBA" if im.mode in ("RGBA", "LA") else "RGB")
        im.thumbnail((side, side))
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=quality, method=6)
    return buf.getvalue()


def process_image(src: Path, dst: Path, opts) -> None:
    """Routine: longest side <= max_side (never upscale), then WebP lossy (default q80)."""
    if opts.format == "keep" and not opts.max_side:
        shutil.copy2(src, dst)
        return
    from PIL import Image, ImageOps  # lazy: only needed for conversion
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if opts.max_side and max(im.size) > opts.max_side:
            k = opts.max_side / max(im.size)
            im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
        if opts.format == "webp":
            has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
            im = im.convert("RGBA" if has_alpha else "RGB")
            im.save(dst, "WEBP", quality=opts.quality, method=6)
        elif opts.format == "jpeg":
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            im.convert("RGB").save(dst, "JPEG", quality=opts.quality, optimize=True)
        else:
            im.save(dst)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def select(arts: list[RawArticle], only: str | None, titles: dict) -> list[RawArticle]:
    if not only:
        return arts
    wanted = [w.strip().lower() for w in only.split(",") if w.strip()]
    res = []
    for a in arts:
        if any((w.isdigit() and int(w) == a.index) or (not w.isdigit() and w in titles[a.index].lower()) for w in wanted):
            res.append(a)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("html", type=Path, help="HTML exported by InDesign (the whole volume)")
    ap.add_argument("-o", "--out", type=Path, default=None, help="output folder (default: ./out/<html stem>)")
    ap.add_argument("--only", help="comma list of article numbers or title/author substrings (e.g. 1,3 or boschi)")
    ap.add_argument("--list", action="store_true", help="only list the articles detected, write nothing")
    ap.add_argument("--format", choices=["webp", "jpeg", "keep"], default="webp",
                    help="image format (default webp lossy). 'keep' = no conversion")
    ap.add_argument("--quality", type=int, default=80, help="WebP/JPEG quality (default 80)")
    ap.add_argument("--max-side", type=int, default=2000,
                    help="longest side in pixels, never upscaled (default 2000; 0 = no resize)")
    ap.add_argument("--strict-css", action="store_true",
                    help="decide bold/italic ONLY from the CSS cascade (ignore the Strong/Emphasis style names)")
    ap.add_argument("--images-dir", type=Path, action="append", default=[],
                    help="folder with high-quality originals (searched recursively, matched by file name without extension, "
                         "any format); can be repeated. Missing ones fall back to the InDesign export")
    ap.add_argument("--xml", action="store_true", help="also write ojs-import.xml (OJS 3.3 native import: metadata + HTML galley + images)")
    ap.add_argument("--inline-images", action="store_true",
                    help="embed the images in article.html as data URIs (thumbnail + full size): one self-contained galley, "
                         "no separate image files to upload; the file gets heavier")
    ap.add_argument("--inline-max-chars", type=int, default=600000,
                    help="with --inline-images: longest data URI allowed in one attribute (default 600000; "
                         "637000 was tested on OJS, longer values are untested)")
    ap.add_argument("--keep-image-names", action="store_true",
                    help="keep the InDesign/author file names instead of <surname>-fig-NN.webp")
    ap.add_argument("--xml-no-images", action="store_true",
                    help="leave the images out of the XML (tiny file: use it if the upload is too big; add the images by hand)")
    ap.add_argument("--xml-citations", action="store_true",
                    help="include the references as OJS citations (off by default: the journal's own exports have none)")
    ap.add_argument("--xml-split", action="store_true",
                    help="besides the volume file, write one ojs-import.xml inside each article folder")
    ap.add_argument("--section-map", default="Articles=ART,Notes=Notes,Reviews=Reviews",
                    help="section name -> OJS section abbreviation (must exist in the journal). Default: %(default)s")
    ap.add_argument("--default-section", default="ART", help="abbreviation for articles with no section (e.g. Premises)")
    ap.add_argument("--issue-volume", type=int, help="issue volume for <issue_identification>")
    ap.add_argument("--issue-number", help="issue number for <issue_identification>")
    ap.add_argument("--issue-year", type=int, help="issue year for <issue_identification>")
    ap.add_argument("--license-url", default="https://creativecommons.org/licenses/by-nc-nd/4.0",
                    help="licenseUrl written in the XML (default CC BY-NC-ND 4.0, as in the journal's own export)")
    ap.add_argument("--locale", default="en_US", help="metadata locale (default en_US)")
    opts = ap.parse_args(argv)
    opts.dry = opts.list
    opts.images_index = index_images(opts.images_dir) if not opts.list else []

    src = opts.html.resolve()
    soup = BeautifulSoup(src.read_text(encoding="utf-8"), "lxml")
    link = soup.find("link", rel="stylesheet")
    css = (src.parent / unquote(link["href"])) if link and link.get("href") else None
    styles = Styles(css, opts.strict_css)
    if not styles.rules:
        print("WARNING: CSS not found, bold/italic will be lost", file=sys.stderr)

    raws = split_volume(soup)
    metas = {}
    for r in raws:
        a = Article(r.index, r.section)
        extract_metadata(r, a)
        metas[r.index] = a.title
    chosen = select(raws, opts.only, {i: t + " " + " ".join(x.name for x in extract_authors_only(raws[i - 1])) for i, t in metas.items()})

    if opts.list:
        for r in raws:
            a = Article(r.index, r.section)
            extract_metadata(r, a)
            mark = "*" if r in chosen else " "
            print(f"{mark}{r.index:2} [{r.section:8}] {a.title[:70]}  —  {'; '.join(x.name for x in a.authors)}")
        return 0

    out_dir = (opts.out or Path("out") / src.stem).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    articles: list[Article] = []
    art_dirs: dict[str, Path] = {}
    for raw in chosen:
        art = Article(raw.index, raw.section)
        extract_metadata(raw, art)
        surname = slugify(art.authors[0].name.split()[-1], 1) if art.authors else "anon"
        art.folder = f"{raw.index:02d}-{surname}-{slugify(art.title)}"
        art_dir = out_dir / art.folder
        art_dir.mkdir(parents=True, exist_ok=True)
        html_doc = build_article(raw, art, soup, styles, src.parent, art_dir, opts)
        (art_dir / "article.html").write_text(html_doc, encoding="utf-8")
        (art_dir / "references.txt").write_text("\n".join(art.references) + ("\n" if art.references else ""), encoding="utf-8")
        (art_dir / "metadata.json").write_text(json.dumps(asdict(art), ensure_ascii=False, indent=2), encoding="utf-8")
        articles.append(art)
        art_dirs[art.folder] = art_dir
        print(f"✓ {art.folder}  ({len(art.images)} img, {len(art.references)} refs, {len(art.warnings)} warnings)")

    write_volume_files(out_dir, articles, src.name)
    if opts.xml:
        write_ojs_xml(out_dir, articles, art_dirs, opts)
        write_ojs_xml(out_dir, articles, art_dirs, opts, "ojs-import-metadata.xml", with_files=False)
        if opts.xml_split:
            for a in articles:
                write_ojs_xml(art_dirs[a.folder], [a], art_dirs, opts)
    print(f"\nOutput in {out_dir}")
    return 0


# ----------------------------------------------------------------------------
# OJS 3.3 native XML import
# ----------------------------------------------------------------------------
def write_ojs_xml(out_dir: Path, articles: list[Article], art_dirs: dict, opts, filename: str = "ojs-import.xml", with_files: bool = True) -> None:
    """One ojs-import.xml with every selected article (metadata, HTML galley, dependent images)."""
    import base64
    import mimetypes
    from datetime import date
    import xml.etree.ElementTree as ET

    NS = "http://pkp.sfu.ca"
    XSI = "http://www.w3.org/2001/XMLSchema-instance"
    ET.register_namespace("", NS)
    ET.register_namespace("xsi", XSI)
    q = lambda tag: f"{{{NS}}}{tag}"
    loc = opts.locale
    today = date.today().isoformat()
    sec_map = dict(kv.split("=", 1) for kv in opts.section_map.split(",") if "=" in kv)
    ids = iter(range(1, 10**6))

    def sub(parent, tag, text=None, **attrs):
        el = ET.SubElement(parent, q(tag), {k: str(v) for k, v in attrs.items()})
        if text is not None:
            el.text = str(text)
        return el

    root = ET.Element(q("articles"), {f"{{{XSI}}}schemaLocation": f"{NS} native.xsd"})
    for art in articles:
        d = art_dirs[art.folder]
        section = sec_map.get(art.section, opts.default_section)
        # The importer makes a publication "current" only when <article current_publication_id> equals the
        # publication's <id type="internal">; without it the public article page and the preview fail (HTTP 500).
        a = sub(root, "article", stage="production", locale=loc, date_submitted=today,
                current_publication_id=art.index)

        def add_file(name, path, genre, stage, parent_id=None):
            fid = next(ids)
            data = path.read_bytes()
            sf = sub(a, "submission_file", id=fid, file_id=fid, stage=stage, genre=genre, viewable="true")
            sub(sf, "name", name, locale=loc)
            if parent_id:
                sub(sf, "submission_file_ref", id=parent_id)
            f = sub(sf, "file", id=fid, filesize=len(data), extension=path.suffix.lstrip(".").lower())
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            e = sub(f, "embed", base64.b64encode(data).decode("ascii"), encoding="base64", mime_type=mime)
            return fid

        html_id = None
        if with_files:
            html_id = add_file("article.html", d / "article.html", "Article Text", "proof")
            for img in ([] if opts.xml_no_images else art.images):
                add_file(img, d / "images" / img, "Image", "dependent", parent_id=html_id)

        pub = sub(a, "publication", locale=loc, version=1, status=1, seq=art.index - 1,
                  section_ref=section, access_status=0)
        sub(pub, "id", str(art.index), type="internal", advice="ignore")
        sub(pub, "title", art.title, locale=loc)
        if art.abstract:
            sub(pub, "abstract", "".join(f"<p>{htmllib.escape(x)}</p>" for x in art.abstract.split("\n\n")), locale=loc)
        if opts.license_url:
            sub(pub, "licenseUrl", opts.license_url)
            sub(pub, "copyrightHolder", ", ".join(a.name for a in art.authors), locale=loc)
            if opts.issue_year:
                sub(pub, "copyrightYear", opts.issue_year)
        if art.keywords:
            kws = sub(pub, "keywords", locale=loc)
            for k in art.keywords:
                sub(kws, "keyword", k)
        authors = sub(pub, "authors")
        for n, au in enumerate(art.authors):
            parts = au.name.split()
            given, family = (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else (au.name, "")
            aid = next(ids)
            ael = sub(authors, "author", include_in_browse="true", user_group_ref="Author", seq=n, id=aid)
            if n == 0:
                pub.set("primary_contact_id", str(aid))   # the importer reads it on <publication>, not on <author>
            sub(ael, "givenname", given, locale=loc)
            if family:
                sub(ael, "familyname", family, locale=loc)
            if au.affiliation:
                sub(ael, "affiliation", au.affiliation, locale=loc)
            sub(ael, "email", au.email or "noreply@example.org")
            if not au.email:
                art.warnings.append(f"XML: author “{au.name}” has no email, placeholder noreply@example.org used")
        if with_files:
            gal = sub(pub, "article_galley", locale=loc, approved="true")
            sub(gal, "name", "HTML", locale=loc)
            sub(gal, "seq", "0")
            sub(gal, "submission_file_ref", id=html_id)
        if art.references and opts.xml_citations:
            cits = sub(pub, "citations")
            for r in art.references:
                sub(cits, "citation", r)
        if opts.issue_volume or opts.issue_number or opts.issue_year:
            ii = sub(pub, "issue_identification")
            if opts.issue_volume:
                sub(ii, "volume", opts.issue_volume)
            if opts.issue_number:
                sub(ii, "number", opts.issue_number)
            if opts.issue_year:
                sub(ii, "year", opts.issue_year)

    ET.indent(root)
    path = out_dir / filename
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    print(f"OJS import file: {path} ({path.stat().st_size // 1024} KB)")


def extract_authors_only(raw: RawArticle):
    a = Article(raw.index, raw.section)
    extract_metadata(raw, a)
    return a.authors


def write_volume_files(out_dir: Path, articles: list[Article], source: str) -> None:
    (out_dir / "volume-metadata.json").write_text(
        json.dumps({"source": source, "articles": [asdict(a) for a in articles]}, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "volume-metadata.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["n", "section", "folder", "title", "authors", "emails", "affiliations", "keywords", "abstract", "n_images", "n_references"])
        for a in articles:
            w.writerow([a.index, a.section, a.folder, a.title,
                        " | ".join(x.name for x in a.authors), " | ".join(x.email for x in a.authors),
                        " | ".join(x.affiliation for x in a.authors), "; ".join(a.keywords),
                        a.abstract, len(a.images), len(a.references)])
    lines = [f"# Conversion report — {source}\n"]
    for a in articles:
        lines.append(f"## {a.index}. {a.title}\n`{a.folder}` — {a.section}, {len(a.images)} images, {len(a.references)} references\n")
        lines += [f"- ⚠ {w}" for w in a.warnings] or ["- OK, no warnings"]
        lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
