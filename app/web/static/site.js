function cloneFrom(id, selector) {
  return document.getElementById(id).content.querySelector(selector).cloneNode(true);
}

function refreshFieldCards(form) {
  form.querySelectorAll("[data-field]").forEach(function (card) {
    var requirement = card.querySelector("[data-requirement]").value;
    card.querySelector("[data-when-equals]").hidden = requirement !== "equals";
    card.querySelector("[data-when-any]").hidden = requirement !== "any";
    var type = card.querySelector("[data-type]").value;
    card.querySelector("[data-choices]").hidden = type !== "choice" && type !== "choices";
    card.querySelector("[data-choice-hint]").hidden = type !== "choice";
    card.querySelector("[data-choices-hint]").hidden = type !== "choices";
    card.querySelector("[data-text-flags]").hidden = type !== "text";
  });
}

function renumber(form) {
  form.querySelectorAll("[data-section]").forEach(function (section, index) {
    section.querySelectorAll("[data-section-index]").forEach(function (input) {
      input.value = String(index);
    });
  });
  refreshFieldCards(form);
}

function applySwitchVisibility(form) {
  form.querySelectorAll("[data-show-field]").forEach(function (el) {
    var control = form.querySelector('[name="' + CSS.escape(el.dataset.showField) + '"]');
    el.hidden = !(control && control.value === el.dataset.showValue);
  });
}

document.querySelectorAll("form[data-confirm]").forEach(function (form) {
  form.addEventListener("submit", function (event) {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});

document.querySelectorAll("a[data-confirm]").forEach(function (link) {
  link.addEventListener("click", function (event) {
    if (!window.confirm(link.dataset.confirm)) event.preventDefault();
  });
});

function copyHint() {
  var platform = navigator.platform || "";
  var agent = navigator.userAgent || "";
  if (/Mac|iPhone|iPad/.test(platform) || /Mac|iPhone|iPad/.test(agent)) return "Press Command-C";
  return "Press Ctrl-C";
}

function selectForCopy(area) {
  area.focus();
  area.select();
}

document.querySelectorAll("[data-copy]").forEach(function (button) {
  button.addEventListener("click", function () {
    var area = document.getElementById(button.dataset.copy);
    var label = button.textContent;
    var done = function (text) {
      button.textContent = text;
      window.setTimeout(function () { button.textContent = label; }, 1600);
    };
    selectForCopy(area);
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(area.value).then(function () { done("Copied"); }).catch(function () {
        done(copyHint());
      });
      return;
    }
    done(copyHint());
  });
});

var instructions = document.getElementById("instructions");
var result = document.getElementById("result");
var formErrors = document.getElementById("form-errors");
var landing = (result && instructions) ? instructions : (result || formErrors);
if (landing) {
  landing.scrollIntoView({block: "start"});
}

document.querySelectorAll("[data-switch-form]").forEach(function (form) {
  var refresh = function () { applySwitchVisibility(form); };
  form.addEventListener("change", refresh);
  refresh();
});

var editor = document.getElementById("editor-form");
if (editor) {
  var status = document.getElementById("editor-status");
  renumber(editor);
  editor.addEventListener("change", function () { refreshFieldCards(editor); });
  function moveElement(node, direction) {
    var sibling = direction === "up" ? node.previousElementSibling : node.nextElementSibling;
    if (!sibling) return false;
    if (direction === "up") node.parentNode.insertBefore(node, sibling);
    else node.parentNode.insertBefore(sibling, node);
    return true;
  }
  editor.addEventListener("click", function (event) {
    var moveField = event.target.closest("[data-move-field]");
    if (moveField) {
      var direction = moveField.getAttribute("data-move-field");
      if (moveElement(moveField.closest("[data-field]"), direction)) status.textContent = "";
      else status.textContent = direction === "up" ? "This field is already first." : "This field is already last.";
      renumber(editor);
      return;
    }
    var moveSection = event.target.closest("[data-move-section]");
    if (moveSection) {
      var sectionDirection = moveSection.getAttribute("data-move-section");
      if (moveElement(moveSection.closest("[data-section]"), sectionDirection)) status.textContent = "";
      else status.textContent = sectionDirection === "up" ? "This section is already first." : "This section is already last.";
      renumber(editor);
      return;
    }
    var addField = event.target.closest("[data-add-field]");
    if (addField) {
      addField.closest("[data-section]").querySelector("[data-fields]").appendChild(cloneFrom("field-template", "[data-field]"));
      renumber(editor);
      return;
    }
    var removeField = event.target.closest("[data-remove-field]");
    if (removeField) {
      var section = removeField.closest("[data-section]");
      if (section.querySelectorAll("[data-field]").length === 1) {
        status.textContent = "A section keeps at least one field.";
        return;
      }
      removeField.closest("[data-field]").remove();
      renumber(editor);
      return;
    }
    var removePaste = event.target.closest("[data-remove-paste-block]");
    if (removePaste) {
      removePaste.closest("[data-paste-block]").remove();
      return;
    }
    var removeSection = event.target.closest("[data-remove-section]");
    if (removeSection) {
      if (editor.querySelectorAll("[data-section]").length === 1) {
        status.textContent = "Keep at least one section.";
        return;
      }
      removeSection.closest("[data-section]").remove();
      renumber(editor);
    }
  });
  var addPaste = document.getElementById("add-paste-block");
  if (addPaste) {
    addPaste.addEventListener("click", function () {
      var max = Number(addPaste.dataset.max || "6");
      if (editor.querySelectorAll("[data-paste-block]").length >= max) {
        status.textContent = "A template can have at most " + max + " paste blocks.";
        return;
      }
      document.getElementById("paste-blocks").appendChild(cloneFrom("paste-block-template", "[data-paste-block]"));
    });
  }
  var addSection = document.getElementById("add-section");
  if (addSection) {
    addSection.addEventListener("click", function () {
      var section = cloneFrom("section-template", "[data-section]");
      section.querySelector("[data-fields]").appendChild(cloneFrom("field-template", "[data-field]"));
      document.getElementById("sections").appendChild(section);
      renumber(editor);
    });
  }
}

var workflowForm = document.getElementById("workflow-form");
if (workflowForm) {
  var workflowStatus = document.getElementById("workflow-status");
  function moveWorkflowRow(node, direction) {
    var sibling = direction === "up" ? node.previousElementSibling : node.nextElementSibling;
    if (!sibling) return false;
    if (direction === "up") node.parentNode.insertBefore(node, sibling);
    else node.parentNode.insertBefore(sibling, node);
    return true;
  }
  workflowForm.addEventListener("click", function (event) {
    var move = event.target.closest("[data-move-workflow]");
    if (move) {
      var direction = move.getAttribute("data-move-workflow");
      var row = move.closest("[data-workflow-template]");
      if (moveWorkflowRow(row, direction)) workflowStatus.textContent = "";
      else workflowStatus.textContent = direction === "up" ? "This template is already first." : "This template is already last.";
      return;
    }
    var remove = event.target.closest("[data-remove-workflow]");
    if (remove) remove.closest("[data-workflow-template]").remove();
  });
  var addWorkflowTemplate = document.getElementById("add-workflow-template");
  if (addWorkflowTemplate) {
    addWorkflowTemplate.addEventListener("click", function () {
      var max = Number(addWorkflowTemplate.dataset.max || "10");
      if (workflowForm.querySelectorAll("[data-workflow-template]").length >= max) {
        workflowStatus.textContent = "A workflow can attach at most " + max + " templates.";
        return;
      }
      document.getElementById("workflow-templates").appendChild(cloneFrom("workflow-template-row", "[data-workflow-template]"));
      workflowStatus.textContent = "";
    });
  }
}
