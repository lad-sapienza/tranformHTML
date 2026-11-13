# INDD2HTML


This programme transforms HTML produced by InDesign to HTML apt to be uploaded as HTML Galley in OJS.

It relies on some convetions in composing the Indeseign file.

## Workflow

1. A `div.article-text#newContainer` is created and added to `body` as first node
2. The `p.Key-words` is used to detect the container of the text
3. The HTML of the old container is put in the new container; the old container is removed
4. Paragraphs with metadata (`.Chapter-heading,.Author,.Author-info,.Key-Words`) are removed from the document.
5. Information on images (filename and captions) are extracted and rearraenged in a gallery container.
  - `p.Caption` is used to detect image containers
  - `img` tag, located as a child of the previous sibling of the caption is used to get image filename, 
6. All div children of body except `div.article-text` are removed
7. `p.Heading-1` are transformed into `h2`
8. `p.Heading-2` are transformed into `h3`
9. `span.Bold-Italic` are transformed into `<strong><em>`
10. `span.Bold` are transformed into `<strong>`
11. `span.Italic` are transformed into `<em>`
12. `p.bibliography` is transformed into `ul li.bibliography`
13. Footnotes at the end of the document are simplified.
  - `div._idFootnote` is used to detect footnotes
  - `a` is used to detect footnote number inside a `div._idFootnote`
14. Footnotes references in the text are semplified:
  - `a._idFootnoteLink` is used to detect footnote references in the text
15. `p.Normal` is simplified into `p`
16. `span.Regular` tags are removed.
17. Text patterns such as Figure n or Plate n are replaced with links