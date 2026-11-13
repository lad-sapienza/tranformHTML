/**
 * Gets all p.bibliography elements from input HTML Element,
 * and reformats these elements in a HTML UL Element
 * Attaches the new UL Element to the input HTML Element (replacing the forst p.bibliography),
 * and removes all useless p.bibliography elements
 * 
 * @param {HTMLElement} htmlElement HTML Element containing original HTML code
 * @returns {HTMLElement} HTML Element with formatted bibliography appended
 */
export default function formatBibliography (htmlElement) {
  
  // Create bibliography ul container with className bibliography
  const biblioCont = document.createElement('ul');
  biblioCont.classList.add('bibliography');

  // Loop into paragraph with classNale bibliography, and for each of them:
  for (const el of htmlElement.querySelectorAll('p.bibliography') ){
    
    // Create a new listi item element
    const li = document.createElement('li');
    
    // Add to list item the inner HTML of the old paragraph
    li.innerHTML = el.innerHTML;

    // Add list item to the bibliography container
    biblioCont.append(li);
  }

  // Replace first paragraph with className bibliography with new bibliography container: a hack
  htmlElement.querySelectorAll('p.bibliography')[0].outerHTML = biblioCont.outerHTML;

  // Remove all paragraphs with className bibliography
  for (const el of htmlElement.querySelectorAll('p.bibliography') ){
   el.remove();
  }

  return htmlElement;
}