/**
 * Gets all footnotes texts from original HTML element by looking for div._idFootnote,
 * removes note number from note text,
 * and reformats notes text by applying the template.
 * 
 * Gets all in-text note references by looking for a._idFootnoteLink,
 * and applies the new template to each of them
 * 
 * Returns the updated HTML Element
 * @param {HTMLElement} htmlElement HTML Element containing original HTML code
 * @returns {HTMLElement} HTML Element with formatted footnotes appended
 */
export default function formatFootNotes (htmlElement) {

  // Get all div elements with className _idFootnote (footnotes text at the end of the document), and for each of them:
  for (const el of htmlElement.querySelectorAll('div._idFootnote')){

    // Get inner text of a element: this is the note number
    const noteNr = el.querySelector('a').innerText;

    // Remove unwanted a element: this is needed to remove note number form note text, see below
    el.querySelector('a').remove();
    for (const e of el.querySelectorAll('span.FootnoteNo')){
      e.remove();
    }
    for (const e of el.querySelectorAll('p.Footnotetxt')){
      e.outerHTML = e.innerHTML;
    }

    // Get foot note HTML, without note number, and make it the new contents of el
    const noteText = el.innerHTML;

    el.outerHTML = `
      <p class="footnote-foot" id="note-${noteNr}">
        <a id="ft-note-${noteNr}" href="#bd-node-${noteNr}">${noteNr}</a>
        <div class="footnote-text" id="note-text-${noteNr}">${noteText}</div>
      </p>
    `;
  }
  
  // Get all a elements with className _idFootnoteLink (in line footnotes references), and for each of them:
  for (const el of htmlElement.querySelectorAll('a._idFootnoteLink') ){
    
    // Get inner text of a element: this is the note number
    const noteNr = el.innerText;

    // Get foot note HTML form the note liste at the end of the document
    const noteText = htmlElement.querySelector(`#note-text-${noteNr}`).innerHTML;

    // Fromat new template
    el.parentNode.outerHTML = `<a href="javascript:void(0);" class="ftpopover" id="${`bd-note-${noteNr}`}" data-content="${noteText.replace('"', '\"').trim()}">${el.innerText}</a>`;
  }

  return htmlElement;
};