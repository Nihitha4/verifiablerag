/* ═══════════════════════════════════════════════════════════════════════
   VerifiableRAG — frontend script  v2
   Features: streaming answers, markdown, copy/export, feedback (👍👎),
             multi-doc selection, conversation search, keyboard shortcuts
═══════════════════════════════════════════════════════════════════════ */

const API_BASE = "http://localhost:8000";

// Configure marked.js — safe rendering, tables enabled
if (window.marked) {
  marked.setOptions({
    gfm: true,
    breaks: true,
  });
}
function renderMarkdown(text) {
  if (!window.marked) return escHtml(text).replace(/\n/g, "<br>");
  return marked.parse(text || "");
}
function escHtml(str) {
  return str.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// ── DOM refs ──────────────────────────────────────────────────────────────────
const newChatBtn        = document.getElementById("newChatBtn");
const convList          = document.getElementById("convList");
const convSearchInput   = document.getElementById("convSearchInput");
const sidebarToggle     = document.getElementById("sidebarToggle");
const navSidebar        = document.getElementById("navSidebar");
const topbarTitle       = document.getElementById("topbarTitle");
const clearChatBtn      = document.getElementById("clearChatBtn");
const exportConvBtn     = document.getElementById("exportConvBtn");

const chatScroll        = document.getElementById("chatScroll");
const emptyState        = document.getElementById("emptyState");
const chatMessages      = document.getElementById("chatMessages");

const questionInput     = document.getElementById("questionInput");
const askBtn            = document.getElementById("askBtn");
const askStatus         = document.getElementById("askStatus");

const docList           = document.getElementById("docList");
const refreshBtn        = document.getElementById("refreshBtn");
const pdfInput          = document.getElementById("fileInput");
const fileLabel         = document.getElementById("fileLabel");
const uploadBtn         = document.getElementById("uploadBtn");
const uploadStatus      = document.getElementById("uploadStatus");

const selectedBanner    = document.getElementById("selectedBanner");
const selectedBannerName= document.getElementById("selectedBannerName");
const deselectBtn       = document.getElementById("deselectBtn");
const summarizeWrap     = document.getElementById("summarizeWrap");
const summarizeBtn      = document.getElementById("summarizeBtn");
const summarizeStyle    = document.getElementById("summarizeStyle");

const deleteDocModal      = document.getElementById("deleteDocModal");
const deleteDocMsg        = document.getElementById("deleteDocMsg");
const deleteDocCancelBtn  = document.getElementById("deleteDocCancelBtn");
const deleteDocConfirmBtn = document.getElementById("deleteDocConfirmBtn");

const deleteConvModal      = document.getElementById("deleteConvModal");
const deleteConvCancelBtn  = document.getElementById("deleteConvCancelBtn");
const deleteConvConfirmBtn = document.getElementById("deleteConvConfirmBtn");

// ══════════════════════════════════════════════════════════════════════════════
//  STORAGE
// ══════════════════════════════════════════════════════════════════════════════

const STORAGE_KEY = "vrag_conversations";

function loadConversations() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || []; }
  catch { return []; }
}
function saveConversations(c) { localStorage.setItem(STORAGE_KEY, JSON.stringify(c)); }
function newConvId() { return "conv_" + Date.now() + "_" + Math.random().toString(36).slice(2,7); }

// ── State ─────────────────────────────────────────────────────────────────────
let conversations    = loadConversations();
let activeConvId     = null;
let selectedDocIds   = new Set();   // multi-doc support
let selectedDocNames = new Map();   // doc_id → filename
let allDocs          = [];
let pendingDeleteDoc  = null;
let pendingDeleteConv = null;
let convSearchQuery   = "";

// ══════════════════════════════════════════════════════════════════════════════
//  SIDEBAR TOGGLE
// ══════════════════════════════════════════════════════════════════════════════

sidebarToggle.addEventListener("click", () => navSidebar.classList.toggle("collapsed"));

// ══════════════════════════════════════════════════════════════════════════════
//  CONVERSATION SEARCH  (task 8)
// ══════════════════════════════════════════════════════════════════════════════

convSearchInput.addEventListener("input", () => {
  convSearchQuery = convSearchInput.value.trim().toLowerCase();
  renderConvList();
});

// ══════════════════════════════════════════════════════════════════════════════
//  CONVERSATION LIST
// ══════════════════════════════════════════════════════════════════════════════

function renderConvList() {
  convList.innerHTML = "";

  const filtered = [...conversations]
    .reverse()
    .filter((c) => !convSearchQuery || (c.title || "").toLowerCase().includes(convSearchQuery));

  if (filtered.length === 0) {
    const p = document.createElement("p");
    p.className = "conv-empty";
    p.textContent = convSearchQuery ? "No matching conversations." : "No conversations yet.";
    convList.appendChild(p);
    return;
  }

  filtered.forEach((conv) => {
    const item = document.createElement("div");
    item.className = "conv-item" + (conv.id === activeConvId ? " active" : "");
    item.dataset.id = conv.id;

    const title = document.createElement("span");
    title.className = "conv-item-title";
    title.textContent = conv.title || "New conversation";
    title.title = conv.title || "";

    // Turn count badge
    const badge = document.createElement("span");
    badge.className = "conv-turn-badge";
    badge.textContent = (conv.turns || []).length;

    const actions = document.createElement("div");
    actions.className = "conv-item-actions";

    const delBtn = document.createElement("button");
    delBtn.className = "conv-action-btn";
    delBtn.title = "Delete";
    delBtn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M9 6V4h6v2"/></svg>`;
    delBtn.addEventListener("click", (e) => { e.stopPropagation(); openDeleteConvModal(conv.id); });

    actions.appendChild(delBtn);
    item.appendChild(title);
    item.appendChild(badge);
    item.appendChild(actions);
    item.addEventListener("click", () => openConversation(conv.id));
    convList.appendChild(item);
  });
}

// ══════════════════════════════════════════════════════════════════════════════
//  CONVERSATION ACTIONS
// ══════════════════════════════════════════════════════════════════════════════

function openConversation(id) {
  activeConvId = id;
  const conv = conversations.find((c) => c.id === id);
  topbarTitle.textContent = conv?.title || "New conversation";
  renderConvList();
  renderChatMessages();
}

function createNewConversation() {
  const conv = { id: newConvId(), title: "New conversation", createdAt: Date.now(), turns: [] };
  conversations.push(conv);
  saveConversations(conversations);
  openConversation(conv.id);
  updateAskState();
}

newChatBtn.addEventListener("click", createNewConversation);

clearChatBtn.addEventListener("click", () => {
  if (!activeConvId) return;
  const conv = conversations.find((c) => c.id === activeConvId);
  if (!conv || !conv.turns.length) return;
  conv.turns = [];
  conv.title = "New conversation";
  saveConversations(conversations);
  topbarTitle.textContent = "New conversation";
  renderConvList();
  renderChatMessages();
});

// ── Export conversation as Markdown  (task 5) ─────────────────────────────────
exportConvBtn.addEventListener("click", () => {
  const conv = conversations.find((c) => c.id === activeConvId);
  if (!conv || !conv.turns.length) { alert("Nothing to export yet."); return; }

  const lines = [`# ${conv.title}`, `_Exported from VerifiableRAG — ${new Date().toLocaleString()}_`, ""];
  conv.turns.forEach((t, i) => {
    lines.push(`## Q${i+1}: ${t.q}`);
    if (t.docName) lines.push(`_Document: ${t.docName}_`);
    lines.push("");
    if (t.error) {
      lines.push(`**Error:** ${t.error}`);
    } else if (t.data) {
      lines.push(t.data.answer || "");
      if (t.data.sources && t.data.sources.length) {
        lines.push("", "**Sources:**");
        t.data.sources.forEach((s, si) => {
          const p = s.metadata?.page_start;
          lines.push(`${si+1}. ${s.metadata?.filename}${p ? ` — page ${p}` : ""}`);
        });
      }
    }
    lines.push("");
  });

  const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href     = url;
  a.download = (conv.title || "conversation").replace(/[^a-z0-9]/gi, "_").toLowerCase() + ".md";
  a.click();
  URL.revokeObjectURL(url);
});

// ── Delete conversation ───────────────────────────────────────────────────────
function openDeleteConvModal(id) {
  pendingDeleteConv = id;
  deleteConvModal.style.display = "flex";
  deleteConvConfirmBtn.focus();
}
function closeDeleteConvModal() { deleteConvModal.style.display = "none"; pendingDeleteConv = null; }

deleteConvCancelBtn.addEventListener("click", closeDeleteConvModal);
deleteConvModal.addEventListener("click", (e) => { if (e.target === deleteConvModal) closeDeleteConvModal(); });
deleteConvConfirmBtn.addEventListener("click", () => {
  if (!pendingDeleteConv) return;
  conversations = conversations.filter((c) => c.id !== pendingDeleteConv);
  saveConversations(conversations);
  if (activeConvId === pendingDeleteConv) {
    activeConvId = null;
    topbarTitle.textContent = "New conversation";
    chatMessages.innerHTML = "";
    emptyState.style.display = "flex";
  }
  closeDeleteConvModal();
  renderConvList();
  updateAskState();
});

// ══════════════════════════════════════════════════════════════════════════════
//  RENDER CHAT MESSAGES
// ══════════════════════════════════════════════════════════════════════════════

function renderChatMessages() {
  chatMessages.innerHTML = "";
  const conv = conversations.find((c) => c.id === activeConvId);
  if (!conv || !conv.turns.length) { emptyState.style.display = "flex"; return; }
  emptyState.style.display = "none";
  conv.turns.forEach((turn) => chatMessages.appendChild(buildTurnElement(turn)));
  chatScroll.scrollTop = chatScroll.scrollHeight;
}

function buildTurnElement(turn) {
  const wrap = document.createElement("div");
  wrap.className = "chat-turn";

  // User bubble
  const qRow = document.createElement("div");
  qRow.className = "turn-q-row";

  if (turn.docName) {
    const tag = document.createElement("span");
    tag.className = "turn-doc-tag";
    tag.textContent = turn.docName;
    qRow.appendChild(tag);
  }

  const qBubble = document.createElement("div");
  qBubble.className = "turn-question";
  qBubble.textContent = turn.q;
  qRow.appendChild(qBubble);
  wrap.appendChild(qRow);

  // Answer area
  const aArea = document.createElement("div");
  aArea.className = "turn-answer-area";

  if (turn.error) {
    aArea.innerHTML = `<div class="turn-error">⚠ ${escHtml(turn.error)}</div>`;
  } else if (turn.data) {
    buildAnswerDOM(aArea, turn.data, turn);
  } else {
    aArea.innerHTML = `<div class="turn-loading"><span class="spinner"></span> Thinking…</div>`;
  }

  wrap.appendChild(aArea);
  return wrap;
}

// ── Build full answer DOM  (tasks 4, 5, 6) ───────────────────────────────────
function buildAnswerDOM(container, data, turn) {
  // Verdict pills
  const counts = { supported: 0, unsupported: 0, contradicted: 0, abstained: 0 };
  (data.claims || []).forEach((c) => {
    const v = (c.verdict || "unsupported").toLowerCase();
    if (counts[v] !== undefined) counts[v]++;
  });

  const header = document.createElement("div");
  header.className = "turn-answer-header";

  const hasUnsupportedOrContradicted = counts.unsupported > 0 || counts.contradicted > 0;
  const isCorrectAbstention = counts.abstained > 0 && !hasUnsupportedOrContradicted;
  const isFullySupported = counts.supported > 0 && counts.unsupported === 0 && counts.contradicted === 0;

  let riskLabel;
  if (hasUnsupportedOrContradicted) {
    riskLabel = "Hallucination Risk: High";
  } else if (counts.unsupported === 0 && counts.contradicted === 0 && counts.abstained === 0) {
    riskLabel = "Hallucination Risk: None";
  } else {
    riskLabel = "Hallucination Risk: Low";
  }
  if (isCorrectAbstention) {
    riskLabel = "Hallucination Risk: None";
  }

  const scoreLevel = hasUnsupportedOrContradicted ? "high" : "low";

  const factualClaims = (data.claims || []).filter(c => {
    const v = (c.verdict || "").toUpperCase();
    return ["SUPPORTED", "UNSUPPORTED", "CONTRADICTED"].includes(v);
  });
  const citedClaims = factualClaims.filter(c => c.source_ids && c.source_ids.length > 0);
  const citationPct = factualClaims.length > 0
    ? Math.round(100 * citedClaims.length / factualClaims.length)
    : 0;
  const citationLevel = citationPct >= 80 ? "high" : citationPct >= 50 ? "medium" : "low";
  const citationBadge = `<span class="pill pill-citation pill-citation-${citationLevel}" title="Citation Coverage: ${citedClaims.length} of ${factualClaims.length} factual claims have a source citation">
        📎 Citation Coverage: ${citationPct}%
       </span>`;

  header.innerHTML = `
    <span class="pill pill-supported">Supported: ${counts.supported}</span>
    <span class="pill pill-unsupported">Unsupported: ${counts.unsupported}</span>
    <span class="pill pill-contradicted">Contradicted: ${counts.contradicted}</span>
    <span class="pill pill-abstained">Correct Abstention: ${counts.abstained}</span>
    <span class="pill pill-risk pill-risk-${scoreLevel}" title="${riskLabel}">
      🎯 ${riskLabel}
    </span>
    ${citationBadge}`;

  // Copy button  (task 5)
  const copyBtn = document.createElement("button");
  copyBtn.className = "answer-action-btn";
  copyBtn.title = "Copy answer";
  copyBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
  copyBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(data.answer || "").then(() => {
      copyBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
      setTimeout(() => {
        copyBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
      }, 1800);
    });
  });
  header.appendChild(copyBtn);
  container.appendChild(header);

  // Evidence status
  const bar = document.createElement("div");
  bar.className = `evidence-status ${data.abstained ? "limited" : "verified"}`;
  let statusText = `Verified against ${(data.sources || []).length} source passage(s).`;
  if (isCorrectAbstention) {
    statusText = "✓ Correct Abstention";
  } else if (hasUnsupportedOrContradicted) {
    statusText = "⚠ Unsupported claim(s) detected";
  } else if (isFullySupported) {
    statusText = "✓ Fully supported";
  }
  bar.textContent = statusText;
  container.appendChild(bar);

  // Answer box — rendered as markdown  (task 4)
  const answerBox = document.createElement("div");
  answerBox.className = "answer-box markdown-body";
  const renderedHtml = renderMarkdown(data.answer || "");

  // Suspicious Phrase Highlighter — flag classic hallucination-signal phrases
  const SUSPICIOUS_PATTERNS = [
    /\bstudies show\b/gi,
    /\bresearch (shows?|suggests?|indicates?|has shown)\b/gi,
    /\bit is (widely|generally|commonly|well) known\b/gi,
    /\bexperts? (agree|say|believe|suggest)\b/gi,
    /\baccording to experts?\b/gi,
    /\bmany (people|experts?|researchers?|scientists?)\b/gi,
    /\bit has been (proven|shown|demonstrated|established)\b/gi,
    /\beveryone knows?\b/gi,
    /\bobviously\b/gi,
    /\bclearly\b/gi,
    /\bundeniably\b/gi,
    /\bsome sources? (say|suggest|indicate|claim)\b/gi,
  ];

  const highlightSuspicious = (html) => {
    // Work on text nodes only — create a temp div, walk text, wrap matches
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    const walk = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        let text = node.textContent;
        let matched = false;
        for (const pat of SUSPICIOUS_PATTERNS) {
          pat.lastIndex = 0;
          if (pat.test(text)) { matched = true; break; }
        }
        if (matched) {
          const span = document.createElement("span");
          let result = text;
          for (const pat of SUSPICIOUS_PATTERNS) {
            pat.lastIndex = 0;
            result = result.replace(pat, (m) =>
              `<mark class="suspicious-phrase" title="⚠ Unverified claim pattern — not grounded in a cited source">${m}</mark>`
            );
          }
          span.innerHTML = result;
          node.replaceWith(span);
        }
      } else if (node.nodeType === Node.ELEMENT_NODE && node.tagName !== "MARK") {
        [...node.childNodes].forEach(walk);
      }
    };
    [...tmp.childNodes].forEach(walk);
    return tmp.innerHTML;
  };

  answerBox.innerHTML = highlightSuspicious(renderedHtml);
  container.appendChild(answerBox);

  // Feedback row  (task 6)
  const feedbackRow = document.createElement("div");
  feedbackRow.className = "feedback-row";

  const feedbackLabel = document.createElement("span");
  feedbackLabel.className = "feedback-label";
  feedbackLabel.textContent = "Was this helpful?";

  const thumbUp = document.createElement("button");
  thumbUp.className = "feedback-btn" + (turn?.feedback === "up" ? " active-up" : "");
  thumbUp.title = "Helpful";
  thumbUp.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>`;

  const thumbDown = document.createElement("button");
  thumbDown.className = "feedback-btn" + (turn?.feedback === "down" ? " active-down" : "");
  thumbDown.title = "Not helpful";
  thumbDown.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3H10z"/><path d="M17 2h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"/></svg>`;

  const setFeedback = (val) => {
    if (turn) {
      turn.feedback = val;
      saveConversations(conversations);
    }
    thumbUp.className   = "feedback-btn" + (val === "up"   ? " active-up"   : "");
    thumbDown.className = "feedback-btn" + (val === "down" ? " active-down" : "");
  };
  thumbUp.addEventListener("click",   () => setFeedback(turn?.feedback === "up"   ? null : "up"));
  thumbDown.addEventListener("click", () => setFeedback(turn?.feedback === "down" ? null : "down"));

  feedbackRow.appendChild(feedbackLabel);
  feedbackRow.appendChild(thumbUp);
  feedbackRow.appendChild(thumbDown);
  container.appendChild(feedbackRow);

  // Claims accordion
  // Keep a ref to the sources <details> so clicking a claim can open+scroll it.
  let sourcesDetEl = null;
  // Map source_id -> <li> element, built when sources accordion is rendered.
  const sourceElMap = {};

  if (data.claims && data.claims.length) {
    const det = document.createElement("details");
    det.className = "disclosure";
    const sum = document.createElement("summary");
    sum.textContent = "Claim verification";
    det.appendChild(sum);

    const ul = document.createElement("ul");
    ul.className = "claims-list";
    data.claims.forEach((c) => {
      const li = document.createElement("li");
      const verdictClass = (c.verdict || "unsupported").toLowerCase();
      li.className = verdictClass;
      // Store source ids as data attribute for linking
      const ids = (c.source_ids || []);
      if (ids.length) li.dataset.sourceIds = ids.join(",");

      const vl = document.createElement("span");
      vl.className = "verdict";
      vl.textContent = (c.verdict || "?").toUpperCase();

      const ct = document.createElement("span");
      ct.className = "claim-text";
      ct.append(c.claim || "");

      if (ids.length) {
        const em = document.createElement("em");
        em.textContent = `Sources: ${ids.join(", ")}`;
        ct.appendChild(em);
      }
      if (c.reason) {
        const em = document.createElement("em");
        em.textContent = c.reason;
        ct.appendChild(em);
      }
      // Cross-document conflict badge
      if (c.cross_doc_conflict && c.conflict_docs && c.conflict_docs.length >= 2) {
        const badge = document.createElement("span");
        badge.className = "cross-doc-badge";
        badge.title = `This claim draws on multiple documents: ${c.conflict_docs.join(" vs ")}`;
        badge.textContent = `⚠ Conflicting sources (${c.conflict_docs.length} docs)`;
        ct.appendChild(badge);
      }
      li.append(vl, ct);

      // Hover: highlight linked sources
      li.addEventListener("mouseenter", () => {
        if (!ids.length) return;
        ids.forEach((sid) => {
          const el = sourceElMap[sid];
          if (el) el.classList.add("source-linked-highlight", `source-highlight-${verdictClass}`);
        });
      });
      li.addEventListener("mouseleave", () => {
        ids.forEach((sid) => {
          const el = sourceElMap[sid];
          if (el) el.classList.remove(
            "source-linked-highlight", "source-highlight-supported",
            "source-highlight-unsupported", "source-highlight-contradicted"
          );
        });
      });

      // Click: open sources accordion and scroll to first linked source
      li.addEventListener("click", () => {
        if (!ids.length || !sourcesDetEl) return;
        sourcesDetEl.open = true;
        const firstEl = sourceElMap[ids[0]];
        if (firstEl) {
          firstEl.scrollIntoView({ behavior: "smooth", block: "center" });
          firstEl.classList.add("source-linked-highlight", `source-highlight-${verdictClass}`);
          setTimeout(() => firstEl.classList.remove(
            "source-linked-highlight", "source-highlight-supported",
            "source-highlight-unsupported", "source-highlight-contradicted"
          ), 1800);
        }
      });

      ul.appendChild(li);
    });
    det.appendChild(ul);
    container.appendChild(det);
  }

  // Sources accordion
  if (data.sources && data.sources.length) {
    const det = document.createElement("details");
    det.className = "disclosure";
    sourcesDetEl = det; // expose to claims linking above
    const sum = document.createElement("summary");
    sum.textContent = `Sources cited (${data.sources.length})`;
    det.appendChild(sum);

    const ol = document.createElement("ol");
    ol.className = "sources-list";
    data.sources.forEach((s, i) => {
      const li = document.createElement("li");
      const sid = `source_${i + 1}`;
      li.dataset.sourceId = sid;
      sourceElMap[sid] = li; // register for claim linking

      const preview = (s.text||"").length > 220 ? s.text.slice(0,220)+"…" : (s.text||"");
      const ps = s.metadata?.page_start;
      const pe = s.metadata?.page_end;
      const pageLabel = ps ? (ps===pe ? `page ${ps}` : `pages ${ps}–${pe}`) : "page unavailable";
      const tag = document.createElement("span");
      tag.className = "source-tag";
      tag.textContent = `[${sid}] ${s.metadata?.filename||""} — ${pageLabel}`;

      // Reverse hover: highlight all claims that cite this source
      li.addEventListener("mouseenter", () => {
        container.querySelectorAll(".claims-list li[data-source-ids]").forEach((claimEl) => {
          const ids = claimEl.dataset.sourceIds.split(",");
          if (ids.includes(sid)) claimEl.classList.add("claim-reverse-highlight");
        });
      });
      li.addEventListener("mouseleave", () => {
        container.querySelectorAll(".claims-list li.claim-reverse-highlight").forEach((el) => {
          el.classList.remove("claim-reverse-highlight");
        });
      });

      li.append(tag, document.createElement("br"), document.createTextNode(preview));
      ol.appendChild(li);
    });
    det.appendChild(ol);
    container.appendChild(det);
  }
}

// ══════════════════════════════════════════════════════════════════════════════
//  ASK  (non-streaming JSON response)
// ══════════════════════════════════════════════════════════════════════════════

questionInput.addEventListener("input", () => {
  questionInput.style.height = "auto";
  questionInput.style.height = Math.min(questionInput.scrollHeight, 160) + "px";
});
questionInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); askBtn.click(); }
});

function updateAskState() {
  const ok = selectedDocIds.size > 0 && !!activeConvId;
  askBtn.disabled = !ok;
  askBtn.title = selectedDocIds.size === 0
    ? "Select a document first"
    : !activeConvId ? "Start a conversation" : "";
}

askBtn.addEventListener("click", async () => {
  const question = questionInput.value.trim();
  if (!question)             { askStatus.textContent = "Type a question first."; return; }
  if (selectedDocIds.size === 0) { askStatus.textContent = "Select at least one document first."; return; }
  if (!activeConvId)         { createNewConversation(); }

  const conv = conversations.find((c) => c.id === activeConvId);
  if (!conv) return;

  const docName = selectedDocIds.size === 1
    ? (selectedDocNames.get([...selectedDocIds][0]) || "")
    : `${selectedDocIds.size} documents`;

  if (conv.turns.length === 0) {
    conv.title = question.length > 52 ? question.slice(0,50)+"…" : question;
    topbarTitle.textContent = conv.title;
  }

  const turn = { q: question, docName, data: null, error: null, feedback: null };
  conv.turns.push(turn);
  saveConversations(conversations);
  renderConvList();

  emptyState.style.display = "none";
  const turnEl = buildTurnElement(turn);
  chatMessages.appendChild(turnEl);
  chatScroll.scrollTop = chatScroll.scrollHeight;

  askBtn.disabled = true;
  questionInput.value = "";
  questionInput.style.height = "auto";
  askStatus.textContent = "Retrieving evidence and verifying…";

  const aArea = turnEl.querySelector(".turn-answer-area");

  try {
    const response = await fetch(`${API_BASE}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, doc_ids: [...selectedDocIds] }),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: "Request failed" }));
      throw new Error(err.detail || "Request failed");
    }
    const data = await response.json();
    turn.data = data;
    saveConversations(conversations);
    aArea.innerHTML = "";
    buildAnswerDOM(aArea, data, turn);
    askStatus.textContent = data.abstained
      ? "Abstained — insufficient verified evidence."
      : `Answered — verified in ${data.rounds} round(s).`;
  } catch (err) {
    turn.error = err.message;
    saveConversations(conversations);
    aArea.innerHTML = `<div class="turn-error">⚠ ${escHtml(err.message)}</div>`;
    askStatus.textContent = `Error: ${err.message}`;
  } finally {
    updateAskState();
    chatScroll.scrollTop = chatScroll.scrollHeight;
  }
});

// ══════════════════════════════════════════════════════════════════════════════
//  DOCUMENT MANAGEMENT  (task 7 — multi-select)
// ══════════════════════════════════════════════════════════════════════════════

const FILE_TYPE_COLOURS = {
  pdf:  { bg:"#fde8e8", fg:"#9b2c2c" },  docx: { bg:"#e8f0fd", fg:"#1e40af" },
  doc:  { bg:"#e8f0fd", fg:"#1e40af" },  pptx: { bg:"#fdf0e8", fg:"#9a3412" },
  xlsx: { bg:"#e8fdf0", fg:"#166534" },  xls:  { bg:"#e8fdf0", fg:"#166534" },
  txt:  { bg:"#f3f4f6", fg:"#374151" },  md:   { bg:"#f3f4f6", fg:"#374151" },
  png:  { bg:"#f5e8fd", fg:"#6b21a8" },  jpg:  { bg:"#f5e8fd", fg:"#6b21a8" },
  jpeg: { bg:"#f5e8fd", fg:"#6b21a8" },  bmp:  { bg:"#f5e8fd", fg:"#6b21a8" },
  tiff: { bg:"#f5e8fd", fg:"#6b21a8" },  tif:  { bg:"#f5e8fd", fg:"#6b21a8" },
  webp: { bg:"#f5e8fd", fg:"#6b21a8" },
};

function fileTypeBadge(filename) {
  const ext = (filename.split(".").pop()||"").toLowerCase();
  const col = FILE_TYPE_COLOURS[ext] || { bg:"#f3f4f6", fg:"#374151" };
  const b   = document.createElement("span");
  b.className = "file-type-badge";
  b.textContent = ext.toUpperCase();
  b.style.background = col.bg;
  b.style.color = col.fg;
  return b;
}

pdfInput.addEventListener("change", () => {
  fileLabel.textContent = pdfInput.files[0] ? pdfInput.files[0].name : "Choose a file…";
});

async function refreshDocuments() {
  try {
    const res  = await fetch(`${API_BASE}/documents`);
    const data = await res.json();
    allDocs = data.documents || [];
    renderDocList();
  } catch (err) { console.error("Failed to refresh documents", err); }
}

function renderDocList() {
  // Deduplicate by filename — keep last (most recent) upload per name
  const latestByName = new Map();
  allDocs.forEach((d) => latestByName.set(d.filename, d));
  const docs = Array.from(latestByName.values());

  docList.innerHTML = "";
  if (docs.length === 0) {
    const p = document.createElement("p");
    p.className = "doc-empty";
    p.textContent = "No documents indexed yet.";
    docList.appendChild(p);
    updateAskState();
    return;
  }

  docs.forEach((doc) => {
    const isActive = selectedDocIds.has(doc.doc_id);
    const row = document.createElement("div");
    row.className = "doc-item" + (isActive ? " active" : "");
    row.dataset.docId = doc.doc_id;
    row.title = "Click to select  ·  Ctrl+click to add to selection";

    // Checkbox for multi-select
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "doc-checkbox";
    cb.checked = isActive;
    cb.setAttribute("aria-label", "Select " + doc.filename);
    cb.addEventListener("click", (e) => { e.stopPropagation(); toggleDoc(doc.doc_id, doc.filename); });

    const label = document.createElement("span");
    label.className = "doc-item-label";
    label.textContent = doc.filename;
    label.title = doc.filename;

    const meta = document.createElement("span");
    meta.className = "doc-item-meta";
    meta.textContent = `${doc.chunks}`;

    const actions = document.createElement("div");
    actions.className = "doc-item-actions";

    // Export button
    const exportBtn = document.createElement("button");
    exportBtn.type = "button";
    exportBtn.className = "doc-action-btn";
    exportBtn.title = "Download text";
    exportBtn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`;
    exportBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      window.open(`${API_BASE}/export/${doc.doc_id}`, "_blank");
    });

    // Delete button
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "doc-action-btn delete-btn";
    delBtn.title = "Delete document";
    delBtn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M9 6V4h6v2"/></svg>`;
    delBtn.addEventListener("click", (e) => { e.stopPropagation(); openDeleteDocModal(doc.doc_id, doc.filename); });

    actions.appendChild(exportBtn);
    actions.appendChild(delBtn);

    // Single click = exclusive select; Ctrl+click = add/remove from multi-select
    row.addEventListener("click", (e) => {
      if (e.ctrlKey || e.metaKey) {
        toggleDoc(doc.doc_id, doc.filename);
      } else {
        if (isActive && selectedDocIds.size === 1) {
          deselectAllDocs();
        } else {
          selectDocExclusive(doc.doc_id, doc.filename);
        }
      }
    });

    row.appendChild(cb);
    row.appendChild(fileTypeBadge(doc.filename));
    row.appendChild(label);
    row.appendChild(meta);
    row.appendChild(actions);
    docList.appendChild(row);
  });

  // Clean up stale selection
  const allIds = new Set(docs.map((d) => d.doc_id));
  for (const id of [...selectedDocIds]) {
    if (!allIds.has(id)) { selectedDocIds.delete(id); selectedDocNames.delete(id); }
  }
  updateSelectedBanner();
  updateAskState();
}

function toggleDoc(id, name) {
  if (selectedDocIds.has(id)) {
    selectedDocIds.delete(id);
    selectedDocNames.delete(id);
  } else {
    selectedDocIds.add(id);
    selectedDocNames.set(id, name);
  }
  renderDocList();
  updateSelectedBanner();
  updateAskState();
}

function selectDocExclusive(id, name) {
  selectedDocIds.clear();
  selectedDocNames.clear();
  selectedDocIds.add(id);
  selectedDocNames.set(id, name);
  renderDocList();
  updateSelectedBanner();
  updateAskState();
}

function deselectAllDocs() {
  selectedDocIds.clear();
  selectedDocNames.clear();
  renderDocList();
  updateSelectedBanner();
  updateAskState();
}

function updateSelectedBanner() {
  const count = selectedDocIds.size;
  if (count === 0) {
    selectedBanner.style.display = "none";
    summarizeWrap.style.display = "none";
    return;
  }
  selectedBanner.style.display = "flex";
  if (count === 1) {
    const name = [...selectedDocNames.values()][0] || "";
    selectedBannerName.textContent = "📄 " + (name.length > 24 ? name.slice(0,22)+"…" : name);
    summarizeWrap.style.display = "block";
  } else {
    selectedBannerName.textContent = `📚 ${count} documents selected`;
    summarizeWrap.style.display = "none";

    // Source Overlap Warning — detect near-duplicate documents
    const selectedIds = [...selectedDocIds];
    const selectedDocs = allDocs.filter(d => selectedIds.includes(d.doc_id));

    // Check for same filename
    const nameGroups = {};
    selectedDocs.forEach(d => {
      const key = d.filename.trim().toLowerCase();
      nameGroups[key] = (nameGroups[key] || 0) + 1;
    });
    const dupNames = Object.entries(nameGroups).filter(([,c]) => c > 1).map(([k]) => k);

    // Check for same chunk count (strong overlap signal)
    const chunkGroups = {};
    selectedDocs.forEach(d => {
      chunkGroups[d.chunks] = (chunkGroups[d.chunks] || []);
      chunkGroups[d.chunks].push(d.filename);
    });
    const dupChunks = Object.entries(chunkGroups).filter(([,names]) => names.length > 1);

    // Remove any existing overlap warning
    const existing = selectedBanner.querySelector(".overlap-warning");
    if (existing) existing.remove();

    if (dupNames.length > 0 || dupChunks.length > 0) {
      const warn = document.createElement("span");
      warn.className = "overlap-warning";
      const detail = dupNames.length > 0
        ? `"${dupNames[0]}" appears ${nameGroups[dupNames[0]]}×`
        : `${dupChunks[0][1].length} docs have identical chunk count (${dupChunks[0][0]})`;
      warn.innerHTML = `⚠ Possible duplicate: ${detail} — results may double-count evidence
        <button class="overlap-dismiss" title="Dismiss — I know they overlap">Proceed anyway ✕</button>`;
      warn.querySelector(".overlap-dismiss").addEventListener("click", () => warn.remove());
      selectedBanner.appendChild(warn);
    }
  }
}

deselectBtn.addEventListener("click", deselectAllDocs);
refreshBtn.addEventListener("click", refreshDocuments);

// ── Delete document ───────────────────────────────────────────────────────────
function openDeleteDocModal(docId, filename) {
  pendingDeleteDoc = { docId, filename };
  deleteDocMsg.textContent = `"${filename}" and all its indexed chunks will be permanently removed.`;
  deleteDocModal.style.display = "flex";
  deleteDocConfirmBtn.focus();
}
function closeDeleteDocModal() { deleteDocModal.style.display = "none"; pendingDeleteDoc = null; }

deleteDocCancelBtn.addEventListener("click", closeDeleteDocModal);
deleteDocModal.addEventListener("click", (e) => { if (e.target === deleteDocModal) closeDeleteDocModal(); });
deleteDocConfirmBtn.addEventListener("click", async () => {
  if (!pendingDeleteDoc) return;
  const { docId, filename } = pendingDeleteDoc;
  closeDeleteDocModal();
  try {
    const res = await fetch(`${API_BASE}/documents/${docId}`, { method: "DELETE" });
    if (!res.ok) throw new Error((await res.json()).detail || "Delete failed");
    selectedDocIds.delete(docId);
    selectedDocNames.delete(docId);
    await refreshDocuments();
  } catch (err) { alert(`Could not delete "${filename}": ${err.message}`); }
});

// ── Upload ─────────────────────────────────────────────────────────────────────
uploadBtn.addEventListener("click", async () => {
  const file = pdfInput.files[0];
  if (!file) { uploadStatus.textContent = "Choose a file first."; return; }
  uploadBtn.disabled = true;

  // Warn user upfront if the file is large
  const sizeMB = file.size / (1024 * 1024);
  const isLarge = sizeMB > 5;

  // Animated dots so the user knows work is in progress
  const dots = ["", ".", "..", "..."];
  let dotIdx = 0;
  const baseMsg = isLarge
    ? `Indexing large file (${sizeMB.toFixed(1)} MB — may take several minutes)`
    : "Uploading and indexing";
  uploadStatus.textContent = baseMsg + "…";
  const progressTimer = setInterval(() => {
    dotIdx = (dotIdx + 1) % dots.length;
    uploadStatus.textContent = baseMsg + dots[dotIdx];
  }, 500);

  const form = new FormData();
  form.append("file", file);

  // 600-second timeout — large textbooks (800+ pages) need several minutes to embed on CPU
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 600_000);

  try {
    const res = await fetch(`${API_BASE}/upload`, {
      method: "POST",
      body: form,
      signal: controller.signal,
    });
    clearTimeout(timeoutId);
    if (!res.ok) throw new Error((await res.json()).detail || "Upload failed");
    const data = await res.json();
    uploadStatus.textContent = `Indexed — ${data.chunks_indexed} chunks.`;
    await refreshDocuments();
    selectDocExclusive(data.doc_id, data.filename);
    pdfInput.value = "";
    fileLabel.textContent = "Choose a file…";
  } catch (err) {
    clearTimeout(timeoutId);
    if (err.name === "AbortError") {
      uploadStatus.textContent = "Timed out after 10 min. Backend may still be processing — wait 30 s then refresh to see if it appears in the document list.";
    } else {
      uploadStatus.textContent = `Error: ${err.message}`;
    }
  } finally {
    clearInterval(progressTimer);
    uploadBtn.disabled = false;
  }
});

// ── Summarize ─────────────────────────────────────────────────────────────────
summarizeBtn.addEventListener("click", async () => {
  const docId = [...selectedDocIds][0];
  if (!docId) return;
  const style = summarizeStyle.value;

  if (!activeConvId) createNewConversation();
  const conv = conversations.find((c) => c.id === activeConvId);
  if (!conv) return;

  const label = { concise:"Concise summary", detailed:"Detailed overview", bullets:"Key bullet points" }[style] || "Summary";
  const question = `${label} of this document`;

  if (conv.turns.length === 0) {
    conv.title = question;
    topbarTitle.textContent = question;
  }

  const turn = { q: question, docName: selectedDocNames.get(docId)||"", data: null, error: null, feedback: null };
  conv.turns.push(turn);
  saveConversations(conversations);
  renderConvList();

  emptyState.style.display = "none";
  const turnEl = buildTurnElement(turn);
  chatMessages.appendChild(turnEl);
  chatScroll.scrollTop = chatScroll.scrollHeight;
  summarizeBtn.disabled = true;
  askStatus.textContent = "Generating summary…";

  try {
    const res = await fetch(`${API_BASE}/summarize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doc_id: docId, style }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "Summarize failed");
    const data = await res.json();
    turn.data = { answer: data.summary, claims: [], sources: [], abstained: false, rounds: 0 };
    saveConversations(conversations);
    const aArea = turnEl.querySelector(".turn-answer-area");
    aArea.innerHTML = "";
    buildAnswerDOM(aArea, turn.data, turn);
    askStatus.textContent = "Summary generated.";
  } catch (err) {
    turn.error = err.message;
    saveConversations(conversations);
    turnEl.querySelector(".turn-answer-area").innerHTML = `<div class="turn-error">⚠ ${escHtml(err.message)}</div>`;
    askStatus.textContent = `Error: ${err.message}`;
  } finally {
    summarizeBtn.disabled = false;
    chatScroll.scrollTop = chatScroll.scrollHeight;
  }
});

// ══════════════════════════════════════════════════════════════════════════════
//  AUTH
// ══════════════════════════════════════════════════════════════════════════════

const AUTH_KEY  = "vrag_auth";
const USERS_KEY = "vrag_users";
function loadAuth()  { try { return JSON.parse(localStorage.getItem(AUTH_KEY)) || null; } catch { return null; } }
function saveAuth(u) { if (u) localStorage.setItem(AUTH_KEY, JSON.stringify(u)); else localStorage.removeItem(AUTH_KEY); }
function loadUsers() { try { return JSON.parse(localStorage.getItem(USERS_KEY)) || {}; } catch { return {}; } }
function saveUsers(u){ localStorage.setItem(USERS_KEY, JSON.stringify(u)); }

let currentUser = loadAuth();

const userMenuBtn        = document.getElementById("userMenuBtn");
const userMenu           = document.getElementById("userMenu");
const userAvatar         = document.getElementById("userAvatar");
const userDisplayName    = document.getElementById("userDisplayName");
const menuLoggedOut      = document.getElementById("menuLoggedOut");
const menuLoggedIn       = document.getElementById("menuLoggedIn");
const menuAvatarLg       = document.getElementById("menuAvatarLg");
const menuLoggedInName   = document.getElementById("menuLoggedInName");
const menuLoggedInEmail  = document.getElementById("menuLoggedInEmail");
const menuLoginBtn       = document.getElementById("menuLoginBtn");
const menuSignupBtn      = document.getElementById("menuSignupBtn");
const menuLogoutBtn      = document.getElementById("menuLogoutBtn");
const authModal          = document.getElementById("authModal");
const authCloseBtn       = document.getElementById("authCloseBtn");
const authLoginForm      = document.getElementById("authLoginForm");
const authSignupForm     = document.getElementById("authSignupForm");
const loginEmail         = document.getElementById("loginEmail");
const loginPassword      = document.getElementById("loginPassword");
const loginError         = document.getElementById("loginError");
const loginSubmitBtn     = document.getElementById("loginSubmitBtn");
const switchToSignup     = document.getElementById("switchToSignup");
const guestBtn           = document.getElementById("guestBtn");
const signupName         = document.getElementById("signupName");
const signupEmail        = document.getElementById("signupEmail");
const signupPassword     = document.getElementById("signupPassword");
const signupError        = document.getElementById("signupError");
const signupSubmitBtn    = document.getElementById("signupSubmitBtn");
const switchToLogin      = document.getElementById("switchToLogin");
const guestBtn2          = document.getElementById("guestBtn2");

function renderAuthState() {
  if (currentUser) {
    const init = (currentUser.name||currentUser.email||"U")[0].toUpperCase();
    userAvatar.textContent = init; userDisplayName.textContent = currentUser.name||currentUser.email;
    menuAvatarLg.textContent = init; menuLoggedInName.textContent = currentUser.name||"User";
    menuLoggedInEmail.textContent = currentUser.email||"";
    menuLoggedOut.style.display = "none"; menuLoggedIn.style.display = "block";
  } else {
    userAvatar.textContent = "G"; userDisplayName.textContent = "Guest";
    menuLoggedOut.style.display = "block"; menuLoggedIn.style.display = "none";
  }
}

userMenuBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  const open = userMenu.style.display !== "none";
  userMenu.style.display = open ? "none" : "block";
  userMenuBtn.setAttribute("aria-expanded", String(!open));
});
document.addEventListener("click", (e) => {
  if (!userMenu.contains(e.target) && e.target !== userMenuBtn) {
    userMenu.style.display = "none";
    userMenuBtn.setAttribute("aria-expanded","false");
  }
});

function openAuthModal(tab="login") {
  userMenu.style.display = "none";
  authLoginForm.style.display  = tab==="login"  ? "block" : "none";
  authSignupForm.style.display = tab==="signup" ? "block" : "none";
  loginError.textContent = ""; signupError.textContent = "";
  authModal.style.display = "flex";
  if (tab==="login") loginEmail.focus(); else signupName.focus();
}
function closeAuthModal() { authModal.style.display = "none"; }

menuLoginBtn.addEventListener("click",  () => openAuthModal("login"));
menuSignupBtn.addEventListener("click", () => openAuthModal("signup"));
authCloseBtn.addEventListener("click",  closeAuthModal);
authModal.addEventListener("click", (e) => { if(e.target===authModal) closeAuthModal(); });
switchToSignup.addEventListener("click", () => { authLoginForm.style.display="none"; authSignupForm.style.display="block"; signupName.focus(); });
switchToLogin.addEventListener("click",  () => { authSignupForm.style.display="none"; authLoginForm.style.display="block"; loginEmail.focus(); });
[guestBtn, guestBtn2].forEach((b) => b.addEventListener("click", closeAuthModal));

loginSubmitBtn.addEventListener("click", () => {
  const email=loginEmail.value.trim(), pass=loginPassword.value;
  loginError.textContent="";
  if (!email||!pass) { loginError.textContent="Please fill in all fields."; return; }
  const users=loadUsers(), user=users[email.toLowerCase()];
  if (!user||user.password!==btoa(pass)) { loginError.textContent="Incorrect email or password."; return; }
  currentUser={name:user.name,email}; saveAuth(currentUser); renderAuthState(); closeAuthModal();
});

signupSubmitBtn.addEventListener("click", () => {
  const name=signupName.value.trim(), email=signupEmail.value.trim(), pass=signupPassword.value;
  signupError.textContent="";
  if (!name||!email||!pass) { signupError.textContent="Please fill in all fields."; return; }
  if (pass.length<6) { signupError.textContent="Password must be at least 6 characters."; return; }
  const users=loadUsers();
  if (users[email.toLowerCase()]) { signupError.textContent="Account already exists."; return; }
  users[email.toLowerCase()]={name,password:btoa(pass)}; saveUsers(users);
  currentUser={name,email}; saveAuth(currentUser); renderAuthState(); closeAuthModal();
});

menuLogoutBtn.addEventListener("click", () => {
  currentUser=null; saveAuth(null); userMenu.style.display="none"; renderAuthState();
});

renderAuthState();

// ══════════════════════════════════════════════════════════════════════════════
//  KEYBOARD SHORTCUTS  (task 9)
// ══════════════════════════════════════════════════════════════════════════════

document.addEventListener("keydown", (e) => {
  // Ignore when typing in an input/textarea
  const tag = document.activeElement?.tagName?.toLowerCase();
  const inInput = tag === "input" || tag === "textarea" || document.activeElement?.isContentEditable;

  // Ctrl+K or Cmd+K — new conversation
  if ((e.ctrlKey || e.metaKey) && e.key === "k") {
    e.preventDefault();
    createNewConversation();
    questionInput.focus();
    return;
  }

  // Escape — close any open modal/menu
  if (e.key === "Escape") {
    closeAuthModal();
    closeDeleteConvModal();
    closeDeleteDocModal();
    userMenu.style.display = "none";
    userMenuBtn.setAttribute("aria-expanded","false");
    return;
  }

  if (inInput) return;

  // / — focus the question input
  if (e.key === "/") {
    e.preventDefault();
    questionInput.focus();
    return;
  }

  // ? — show keyboard shortcut hint
  if (e.key === "?") {
    askStatus.textContent = "Shortcuts: / = focus input  ·  Ctrl+K = new chat  ·  Esc = close modal";
    setTimeout(() => { askStatus.textContent = ""; }, 3000);
    return;
  }
});

function closeDeleteDocModal() { deleteDocModal.style.display="none"; pendingDeleteDoc=null; }

// ══════════════════════════════════════════════════════════════════════════════
//  BOOTSTRAP
// ══════════════════════════════════════════════════════════════════════════════

if (conversations.length > 0) {
  openConversation(conversations[conversations.length - 1].id);
} else {
  createNewConversation();
}

refreshDocuments();
updateAskState();
