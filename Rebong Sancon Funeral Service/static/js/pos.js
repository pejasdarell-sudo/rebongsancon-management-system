// RebongSancon Smart POS JavaScript Logic
// Handles:
// 1. ESP32 QR scan events & preset funeral packages
// 2. Custom Order Builder (client picks flowers, casket, lights, venue, embalming, hearse)
// 3. Real-time pricing calculations & down payment management

// =====================================================================
// MODE SWITCHER: PACKAGE vs CUSTOM ORDER
// =====================================================================
let currentPosMode = 'package'; // 'package' or 'custom'

function switchPosMode(mode) {
  currentPosMode = mode;
  const viewPkg = document.getElementById('viewPackageOrder');
  const viewCust = document.getElementById('viewCustomOrder');
  const btnPkg = document.getElementById('tabBtnPackage');
  const btnCust = document.getElementById('tabBtnCustom');

  if (mode === 'custom') {
    if (viewPkg) viewPkg.classList.add('hidden');
    if (viewCust) viewCust.classList.remove('hidden');

    if (btnPkg) {
      btnPkg.className = "px-4 py-2.5 rounded-xl text-xs font-bold transition-all flex items-center gap-2 text-slate-600 hover:text-slate-900";
    }
    if (btnCust) {
      btnCust.className = "px-4 py-2.5 rounded-xl text-xs font-bold transition-all flex items-center gap-2 bg-purple-700 text-white shadow-sm";
    }
    handleCustomSelectionChange();
  } else {
    if (viewPkg) viewPkg.classList.remove('hidden');
    if (viewCust) viewCust.classList.add('hidden');

    if (btnPkg) {
      btnPkg.className = "px-4 py-2.5 rounded-xl text-xs font-bold transition-all flex items-center gap-2 bg-white text-slate-900 shadow-sm";
    }
    if (btnCust) {
      btnCust.className = "px-4 py-2.5 rounded-xl text-xs font-bold transition-all flex items-center gap-2 text-slate-600 hover:text-slate-900";
    }
    calculateTotals();
  }
}

// Format Currency into Philippine Peso (₱)
function formatPHP(amount) {
  return "₱" + Number(amount || 0).toLocaleString('en-PH');
}

// =====================================================================
// SECTION 1: PACKAGE ORDER LOGIC (ESP32 Smart QR & Inclusions)
// =====================================================================
let currentScannedItem = null; // Starts empty: awaits actual QR scan from Mobile App or ESP32
let autoPollInterval = null;
let isPolling = false;
let downPaymentMode = '50'; // '50', '100', or 'custom'

// Audio confirmation chime when a package QR is scanned
function playScanChime() {
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = 'sine';
    osc.frequency.setValueAtTime(784, ctx.currentTime); // G5
    osc.frequency.setValueAtTime(1046.50, ctx.currentTime + 0.08); // C6
    gain.gain.setValueAtTime(0.12, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.22);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + 0.22);
  } catch (e) {
    // Audio may be blocked by browser autoplay policy
  }
}

function renderScannedItem(item) {
  const awaitingState = document.getElementById('awaitingScanState');
  const scannedContainer = document.getElementById('scannedPackageContainer');

  if (!item) {
    currentScannedItem = null;
    if (awaitingState) awaitingState.classList.remove('hidden');
    if (scannedContainer) scannedContainer.classList.add('hidden');
    calculateTotals();
    if (window.lucide) { lucide.createIcons(); }
    return;
  }

  currentScannedItem = item;

  // Reveal scanned package details & hide awaiting scan placeholder
  if (awaitingState) awaitingState.classList.add('hidden');
  if (scannedContainer) scannedContainer.classList.remove('hidden');

  const badge = document.getElementById('badgeCasketCode');
  const cat = document.getElementById('txtCategory');
  const name = document.getElementById('txtPackageName');
  const price = document.getElementById('txtBasePrice');
  const mat = document.getElementById('txtMaterial');
  const dims = document.getElementById('txtDimensions');

  if (badge) badge.textContent = item.casketCode;
  if (cat) cat.textContent = "• " + (item.category || "Funeral Package");
  if (name) name.textContent = item.name;
  if (price) price.textContent = formatPHP(item.basePrice);
  if (mat) mat.textContent = item.material || "Premium Hardwood";
  if (dims) dims.textContent = item.dimensions || 'Standard Full Size';

  const container = document.getElementById('inclusionsContainer');
  if (container) {
    container.innerHTML = '';
    const incList = (item.inclusions && item.inclusions.length > 0) ? item.inclusions : [
      "Professional Embalming & Cosmetic Care",
      "Chapel Vigil Viewing Amenities",
      "Funeral Hearse Transportation"
    ];
    incList.forEach(inc => {
      const div = document.createElement('div');
      div.className = "flex items-start gap-2 bg-slate-50 p-2.5 rounded-xl border border-slate-100 text-xs text-slate-700 font-medium";
      div.innerHTML = `
        <svg class="w-4 h-4 text-emerald-600 shrink-0 mt-0.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"></path>
        </svg>
        <span>${inc}</span>
      `;
      container.appendChild(div);
    });
  }

  calculateTotals();
  if (window.lucide) { lucide.createIcons(); }
}

function setDownPaymentPreset(preset) {
  downPaymentMode = preset;
  const btn50 = document.getElementById('btnDp50');
  const btn100 = document.getElementById('btnDp100');
  const btnCustom = document.getElementById('btnDpCustom');
  const badge = document.getElementById('dpStatusBadge');

  const activeClass = "py-1 px-2 rounded-lg text-[11px] font-bold border border-yellow-500 bg-yellow-50 text-yellow-800 transition-all text-center";
  const inactiveClass = "py-1 px-2 rounded-lg text-[11px] font-semibold border border-slate-200 bg-white text-slate-700 hover:bg-slate-50 transition-all text-center";

  if (btn50) btn50.className = preset === '50' ? activeClass : inactiveClass;
  if (btn100) btn100.className = preset === '100' ? activeClass : inactiveClass;
  if (btnCustom) btnCustom.className = preset === 'custom' ? activeClass : inactiveClass;

  if (badge) {
    if (preset === '50') badge.textContent = "Auto-compute (50%)";
    else if (preset === '100') badge.textContent = "Full Payment (100%)";
    else badge.textContent = "Custom Amount";
  }

  calculateTotals(false);
}

function handleCustomDownPaymentInput() {
  downPaymentMode = 'custom';
  const btn50 = document.getElementById('btnDp50');
  const btn100 = document.getElementById('btnDp100');
  const btnCustom = document.getElementById('btnDpCustom');
  const badge = document.getElementById('dpStatusBadge');

  const activeClass = "py-1 px-2 rounded-lg text-[11px] font-bold border border-yellow-500 bg-yellow-50 text-yellow-800 transition-all text-center";
  const inactiveClass = "py-1 px-2 rounded-lg text-[11px] font-semibold border border-slate-200 bg-white text-slate-700 hover:bg-slate-50 transition-all text-center";

  if (btn50) btn50.className = inactiveClass;
  if (btn100) btn100.className = inactiveClass;
  if (btnCustom) btnCustom.className = activeClass;
  if (badge) badge.textContent = "Custom Amount";

  calculateTotals(true);
}

function calculateTotals(isManualDp = false) {
  const basePrice = currentScannedItem ? (currentScannedItem.basePrice || 0) : 0;

  let addOnsTotal = 0;
  document.querySelectorAll('#addOnsList .addon-checkbox:checked').forEach(cb => {
    addOnsTotal += parseFloat(cb.getAttribute('data-price')) || 0;
  });

  const subtotal = basePrice + addOnsTotal;

  const customDiscInput = document.getElementById('customDiscountInput');
  const discountPercent = customDiscInput ? (parseFloat(customDiscInput.value) || 0) : 0;
  const discountAmount = Math.max(0, (subtotal * discountPercent) / 100);
  const totalAmount = Math.max(0, subtotal - discountAmount);

  const dpInput = document.getElementById('downPaymentInput');
  let downPayment = 0;

  if (downPaymentMode === '50') {
    downPayment = Math.round(totalAmount * 0.50);
    if (dpInput) dpInput.value = downPayment;
  } else if (downPaymentMode === '100') {
    downPayment = totalAmount;
    if (dpInput) dpInput.value = downPayment;
  } else {
    const rawVal = dpInput ? String(dpInput.value).trim() : '0';
    if (rawVal.includes('-')) {
      downPayment = 0;
    } else {
      downPayment = dpInput ? (parseFloat(dpInput.value) || 0) : 0;
      if (Object.is(downPayment, -0)) downPayment = 0;
    }
  }

  const remainingBalance = Math.max(0, totalAmount - downPayment);

  if (document.getElementById('summaryBasePrice')) document.getElementById('summaryBasePrice').textContent = formatPHP(basePrice);
  if (document.getElementById('summaryAddOnsTotal')) document.getElementById('summaryAddOnsTotal').textContent = "+" + formatPHP(addOnsTotal);
  if (document.getElementById('summarySubtotal')) document.getElementById('summarySubtotal').textContent = formatPHP(subtotal);
  if (document.getElementById('summaryDiscount')) {
    document.getElementById('summaryDiscount').textContent = discountAmount > 0 ? `-${formatPHP(discountAmount)} (${discountPercent}%)` : "₱0";
  }
  if (document.getElementById('summaryTotalDue')) document.getElementById('summaryTotalDue').textContent = formatPHP(totalAmount);
  if (document.getElementById('summaryRemainingBalance')) document.getElementById('summaryRemainingBalance').textContent = formatPHP(remainingBalance);
}

async function fetchLatestScan(isManualClick = false) {
  try {
    const res = await fetch('/api/pos/scan');
    if (!res.ok) throw new Error('Failed to retrieve scan buffer');
    const data = await res.json();
    if (data && data.hasScan && data.scannedItem && data.scannedItem.casketCode) {
      if (!currentScannedItem || currentScannedItem.casketCode !== data.scannedItem.casketCode || isManualClick) {
        renderScannedItem(data.scannedItem);
        playScanChime();
        showNotice(`ESP32 Scan Detected: ${data.scannedItem.casketCode} - ${data.scannedItem.name}`, 'success');
      }
    } else if (isManualClick) {
      showNotice('No active scan waiting in buffer. Scan a package tag to load.', 'info');
    }
  } catch (err) {
    if (isManualClick) {
      showNotice('Could not connect to /api/pos/scan. Ensure server is up.', 'error');
    }
  }
}

function toggleAutoPolling(forceStart = null) {
  if (forceStart !== null) {
    isPolling = forceStart;
  } else {
    isPolling = !isPolling;
  }
  const indicator = document.getElementById('pollIndicator');
  const text = document.getElementById('pollText');
  const btn = document.getElementById('btnTogglePoll');

  if (isPolling) {
    if (indicator) indicator.className = "w-2 h-2 rounded-full bg-emerald-500 animate-ping";
    if (text) text.textContent = "ESP32 Auto-Listener (Active)";
    if (btn) btn.className = "px-3.5 py-2 rounded-xl text-xs font-semibold flex items-center gap-2 border bg-emerald-50 border-emerald-300 text-emerald-700 shadow-sm transition-all";
    if (!autoPollInterval) {
      autoPollInterval = setInterval(() => fetchLatestScan(false), 2500);
    }
  } else {
    if (indicator) indicator.className = "w-2 h-2 rounded-full bg-slate-400";
    if (text) text.textContent = "ESP32 Auto-Polling (Paused)";
    if (btn) btn.className = "px-3.5 py-2 rounded-xl text-xs font-semibold flex items-center gap-2 border bg-slate-50 border-slate-200 text-slate-600 hover:bg-slate-100 transition-all";
    if (autoPollInterval) {
      clearInterval(autoPollInterval);
      autoPollInterval = null;
    }
  }
}

async function scanPackageCode(code) {
  if (!code) return;
  const cleanCode = code.trim().toUpperCase();
  try {
    const res = await fetch('/api/pos/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ qrCode: cleanCode })
    });
    const result = await res.json();
    if (res.ok && result.scannedItem) {
      renderScannedItem(result.scannedItem);
      playScanChime();
      showNotice(`Verified & Loaded: ${result.scannedItem.casketCode} - ${result.scannedItem.name}`, 'success');
    } else {
      showNotice(result.message || `Package code '${cleanCode}' not found in Firebase Firestore.`, 'error');
    }
  } catch (err) {
    showNotice('Failed to communicate with QR scanner service.', 'error');
  }
}

function handleManualScan(e) {
  e.preventDefault();
  const input = document.getElementById('manualQrInput');
  if (!input) return;
  const code = input.value.trim();
  if (code) {
    scanPackageCode(code);
    input.value = '';
  }
}

async function clearCurrentScan() {
  try {
    await fetch('/api/pos/scan/clear', { method: 'POST' });
  } catch (e) {
    // silent fallback
  }
  currentScannedItem = null;
  renderScannedItem(null);
  resetPOSFormFields();
  showNotice('Scan cleared. Ready for next package scan.', 'info');
}

function resetPOSFormFields() {
  if (document.getElementById('deceasedName')) document.getElementById('deceasedName').value = '';
  if (document.getElementById('contactPerson')) document.getElementById('contactPerson').value = '';
  if (document.getElementById('contactNumber')) document.getElementById('contactNumber').value = '';
  if (document.getElementById('customDiscountInput')) document.getElementById('customDiscountInput').value = '0';
  ['deceasedNameErr', 'contactPersonErr', 'contactNumberErr', 'customDeceasedNameErr', 'customContactPersonErr', 'customContactNumberErr'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.textContent = ''; el.classList.add('hidden'); }
  });
  document.querySelectorAll('#addOnsList .addon-checkbox:checked').forEach(cb => cb.checked = false);
  setDownPaymentPreset('50');
}

// Live Real-Time Input Validator for Package Billing Summary (blocks 5 in a row like 'yyyyy')
function handleNameInput(inputEl, errElId) {
  if (!inputEl) return;
  const errEl = errElId ? document.getElementById(errElId) : null;
  const val = inputEl.value;

  // Check for 5 or more repeating characters in a row (e.g. 'yyyyy')
  const rep5Match = val.match(/(.)\1{4,}/i);
  if (rep5Match) {
    if (errEl) {
      errEl.textContent = `No repeating characters: maximum 4 in a row allowed (blocked '${rep5Match[0]}').`;
      errEl.classList.remove('hidden');
    }
    // Immediately cap the sequence to 4 characters so 5 in a row ('yyyyy') cannot be entered
    inputEl.value = val.replace(/(.)\1{4,}/gi, (match, char) => char.repeat(4));
    return;
  }

  const rep4Match = val.match(/(.)\1{3,}/i);
  if (rep4Match) {
    if (errEl) {
      errEl.textContent = `Repeated characters detected ('${rep4Match[0]}'). Maximum 4 allowed.`;
      errEl.classList.remove('hidden');
    }
  } else {
    if (errEl) {
      errEl.textContent = '';
      errEl.classList.add('hidden');
    }
  }
}

function handleMobileInput(inputEl, errElId) {
  if (!inputEl) return;
  inputEl.value = inputEl.value.replace(/[^0-9]/g, '').slice(0, 11);
  const val = inputEl.value;
  const errEl = errElId ? document.getElementById(errElId) : null;

  const rep5Match = val.match(/(\d)\1{4,}/);
  if (rep5Match) {
    if (errEl) {
      errEl.textContent = "Mobile cannot contain 5 repeating digits in a row.";
      errEl.classList.remove('hidden');
    }
    inputEl.value = val.replace(/(\d)\1{4,}/g, (match, d) => d.repeat(4));
  } else {
    if (errEl) {
      errEl.textContent = '';
      errEl.classList.add('hidden');
    }
  }
}

function resetPOSForm() {
  clearCurrentScan();
}

// ==============================================================
// SHARED ANTI-HUMAN-ERROR INPUT VALIDATION & FINAL REVIEW MODAL
// ==============================================================
let pendingOrderSubmission = null;
let isSubmittingFinalOrder = false;

const PERSON_NAME_ALLOWED = /^[A-Za-zÀ-ÿñÑ\s.\-']+$/;
const PH_MOBILE_PATTERN = /^09\d{9}$/;

function validatePersonNameClient(name, fieldName, elementId) {
  const clean = (name || '').replace(/\s+/g, ' ').trim();
  if (!clean || clean.length < 2) {
    alert(`Please enter a valid ${fieldName} (at least 2 characters).`);
    if (elementId) document.getElementById(elementId)?.focus();
    return null;
  }

  if (clean.length > 40) {
    alert(`${fieldName} cannot exceed 40 characters (currently ${clean.length} characters).`);
    if (elementId) document.getElementById(elementId)?.focus();
    return null;
  }

  // 1. Forbid numbers
  if (/\d/.test(clean)) {
    alert(`${fieldName} cannot contain numbers. Please use letters only.`);
    if (elementId) document.getElementById(elementId)?.focus();
    return null;
  }

  // 2. Allowed characters only
  if (!PERSON_NAME_ALLOWED.test(clean) || clean.includes('_')) {
    alert(`${fieldName} contains invalid symbols. Only letters, spaces, hyphens (-), apostrophes ('), and periods (.) are allowed.`);
    if (elementId) document.getElementById(elementId)?.focus();
    return null;
  }

  // 3. Cannot start or end with punctuation marks
  if (/^[.\-'\s]/.test(clean) || /[.\-'\s]$/.test(clean)) {
    alert(`${fieldName} cannot start or end with a hyphen, dot, or apostrophe.`);
    if (elementId) document.getElementById(elementId)?.focus();
    return null;
  }

  // 4. No consecutive punctuation
  if (/[.\-']{2,}/.test(clean)) {
    alert(`${fieldName} cannot contain consecutive punctuation marks (e.g., '..', '--').`);
    if (elementId) document.getElementById(elementId)?.focus();
    return null;
  }

  // 5. Strictly block 4+ repeating characters in a row (e.g. 'yyyyy', 'aaaa')
  const repMatch = clean.match(/(.)\1{3,}/i);
  if (repMatch) {
    alert(`${fieldName} cannot contain repeated characters in a row (found '${repMatch[0]}'). Maximum of 4 in a row allowed (no 'yyyyy'). Please enter a realistic name.`);
    if (elementId) document.getElementById(elementId)?.focus();
    return null;
  }

  // 6. Extract alphabetic letters
  const letters = clean.match(/[A-Za-zÀ-ÿñÑ]/g) || [];
  if (letters.length < 2) {
    alert(`${fieldName} must contain at least 2 alphabetic letters.`);
    if (elementId) document.getElementById(elementId)?.focus();
    return null;
  }

  // 7. Check character diversity (prevent "a a a a a", "x y x x x x")
  if (letters.length >= 4) {
    const freq = {};
    let maxCount = 0;
    let maxChar = '';
    for (const ch of letters.map(c => c.toLowerCase())) {
      freq[ch] = (freq[ch] || 0) + 1;
      if (freq[ch] > maxCount) {
        maxCount = freq[ch];
        maxChar = ch;
      }
    }
    if ((maxCount / letters.length) > 0.60) {
      alert(`${fieldName} has too many repeated letters ('${maxChar}'). Please enter a realistic name.`);
      if (elementId) document.getElementById(elementId)?.focus();
      return null;
    }
  }

  // 8. Vowel & pronounceability check for names >= 3 letters
  if (letters.length >= 3) {
    const hasVowel = /[aeiouyÀ-ÿñÑ]/i.test(clean);
    if (!hasVowel) {
      alert(`${fieldName} must contain at least one vowel (a, e, i, o, u, y).`);
      if (elementId) document.getElementById(elementId)?.focus();
      return null;
    }
  }

  // 9. Block test dummy placeholders (e.g. 'test', 'dummy', 'asdf', 'n/a')
  const lowerClean = clean.toLowerCase();
  const dummyWords = new Set(['test', 'testing', 'tester', 'dummy', 'sample', 'example', 'asdf', 'qwerty', 'null', 'none', 'n/a', 'na', 'unknown', 'tba', 'tbd']);
  if (dummyWords.has(lowerClean)) {
    alert(`${fieldName} cannot be a test placeholder (e.g., '${clean}'). Please enter an actual name.`);
    if (elementId) document.getElementById(elementId)?.focus();
    return null;
  }
  const nameTokens = lowerClean.split(/\s+/);
  for (const token of nameTokens) {
    if (dummyWords.has(token)) {
      alert(`${fieldName} contains a test placeholder word ('${token}'). Please enter an actual name.`);
      if (elementId) document.getElementById(elementId)?.focus();
      return null;
    }
  }

  // 10. Prevent strings of disjoint single letters (e.g., 'a b c d e')
  const singleLetters = nameTokens.filter(t => t.length === 1 && !t.endsWith('.'));
  if (singleLetters.length >= 3 || (nameTokens.length >= 3 && singleLetters.length === nameTokens.length)) {
    alert(`${fieldName} appears to be random single letters. Please enter a valid name.`);
    if (elementId) document.getElementById(elementId)?.focus();
    return null;
  }

  return clean;
}

function validateOrderFields({
  deceasedName,
  contactPerson,
  contactNumber,
  discountPercent,
  totalAmount,
  downPayment,
  ids
}) {
  const cleanDeceased = validatePersonNameClient(deceasedName, "Deceased Full Name", ids.deceased);
  if (!cleanDeceased) return null;

  const cleanContact = validatePersonNameClient(contactPerson, "Contact Person / Client Name", ids.contact);
  if (!cleanContact) return null;

  // Normalize mobile number (+63, spaces, hyphens)
  let rawDigits = (contactNumber || '').replace(/[^0-9]/g, '');
  if (rawDigits.startsWith('63') && rawDigits.length === 12) {
    rawDigits = '0' + rawDigits.slice(2);
  } else if (rawDigits.startsWith('9') && rawDigits.length === 10) {
    rawDigits = '0' + rawDigits;
  }

  if (!PH_MOBILE_PATTERN.test(rawDigits)) {
    alert('Please enter a valid 11-digit Philippine mobile number starting with 09 (e.g., 09171234567).');
    document.getElementById(ids.mobile)?.focus();
    return null;
  }

  // Check all identical digits (dummy 09000000000, 09999999999)
  const subscriberDigits = rawDigits.slice(2);
  if (new Set(subscriberDigits.split('')).size <= 1) {
    alert('Mobile number cannot consist of all identical dummy digits (e.g., 09000000000).');
    document.getElementById(ids.mobile)?.focus();
    return null;
  }

  // Reject 5+ repeating digits in a row
  if (/(\d)\1{4,}/.test(rawDigits)) {
    alert('Mobile number cannot contain 5 or more repeating digits in a row.');
    document.getElementById(ids.mobile)?.focus();
    return null;
  }

  const cleanMobile = rawDigits;

  const discElement = ids.discount ? document.getElementById(ids.discount) : null;
  const rawDiscStr = discElement ? String(discElement.value).trim() : '';
  if (rawDiscStr.includes('-') || Object.is(discountPercent, -0) || isNaN(discountPercent) || discountPercent < 0 || discountPercent > 100) {
    alert('Discount percentage must be between 0% and 100% and cannot be negative or contain a negative sign (e.g. -0).');
    discElement?.focus();
    return null;
  }

  if (isNaN(totalAmount) || totalAmount <= 0) {
    alert('Total Order Amount must be greater than ₱0. Please check your selected package and discount.');
    return null;
  }

  const dpElement = ids.downPayment ? document.getElementById(ids.downPayment) : null;
  const rawDpStr = dpElement ? String(dpElement.value).trim() : '';

  if (rawDpStr.includes('-') || Object.is(downPayment, -0) || isNaN(downPayment) || downPayment < 0) {
    alert('Down Payment cannot be negative or contain a negative sign (e.g. -0).');
    dpElement?.focus();
    return null;
  }

  if (downPayment > totalAmount + 0.01) {
    alert(`Down Payment (${formatPHP(downPayment)}) cannot exceed Total Amount (${formatPHP(totalAmount)}). Please check for typing errors.`);
    dpElement?.focus();
    return null;
  }

  // Collect smart human-error warnings for the Final Verification Modal
  const warnings = [];
  if (cleanDeceased.toLowerCase() === cleanContact.toLowerCase()) {
    warnings.push('Deceased Full Name and Contact Person are identical — please confirm this is not a copy-paste error.');
  }
  if (discountPercent > 20) {
    warnings.push(`A high discount of ${discountPercent}% is applied — please verify this discount was authorized.`);
  }
  if (downPayment === 0) {
    warnings.push('Down Payment is ₱0 — the entire balance will be recorded as unpaid.');
  }

  return {
    deceasedName: cleanDeceased,
    contactPerson: cleanContact,
    contactNumber: cleanMobile,
    warnings
  };
}

function openFinalValidationModal(config) {
  pendingOrderSubmission = config;
  const modal = document.getElementById('posFinalValidationModal');
  if (!modal) {
    // Fallback if modal element is absent
    if (confirm(`Confirm ${config.orderType} Order for ${config.display.deceasedName} (${formatPHP(config.display.totalAmount)})?`)) {
      confirmAndSubmitPendingOrder();
    }
    return;
  }

  const badge = document.getElementById('valOrderTypeBadge');
  if (badge) {
    badge.textContent = config.orderType === 'Custom' ? 'Custom Order' : 'Preset Package';
    badge.className = config.orderType === 'Custom'
      ? 'px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-purple-100 text-purple-800'
      : 'px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-amber-100 text-amber-800';
  }

  document.getElementById('valPackageName').textContent = config.display.packageName;
  const addOnsRow = document.getElementById('valAddOnsRow');
  const addOnsText = document.getElementById('valAddOnsText');
  if (config.display.addOns && config.display.addOns.length > 0) {
    addOnsRow.classList.remove('hidden');
    addOnsText.textContent = config.display.addOns.map(a => `${a.name} (${formatPHP(a.price)})`).join(', ');
  } else {
    addOnsRow.classList.add('hidden');
    addOnsText.textContent = '';
  }

  document.getElementById('valDeceasedName').textContent = config.display.deceasedName;
  document.getElementById('valContactPerson').textContent = config.display.contactPerson;
  document.getElementById('valContactNumber').textContent = config.display.contactNumber;

  document.getElementById('valSubtotal').textContent = formatPHP(config.display.subtotal);
  document.getElementById('valDiscount').textContent = config.display.discountAmount > 0
    ? `-${formatPHP(config.display.discountAmount)} (${config.display.discountPercent}%)`
    : '₱0';
  document.getElementById('valTotalAmount').textContent = formatPHP(config.display.totalAmount);
  document.getElementById('valPaymentMethod').textContent = config.display.paymentMethod;
  document.getElementById('valDownPayment').textContent = formatPHP(config.display.downPayment);
  document.getElementById('valRemainingBalance').textContent = formatPHP(config.display.remainingBalance);

  // Render human-error warnings if any
  const warningsBox = document.getElementById('valWarningsBox');
  const warningsList = document.getElementById('valWarningsList');
  if (config.warnings && config.warnings.length > 0) {
    warningsList.innerHTML = config.warnings.map(w => `<li>${w}</li>`).join('');
    warningsBox.classList.remove('hidden');
  } else {
    warningsList.innerHTML = '';
    warningsBox.classList.add('hidden');
  }

  // Reset confirmation checkbox & submit button state
  const checkbox = document.getElementById('valConfirmCheckbox');
  const confirmBtn = document.getElementById('btnValConfirmSubmit');
  if (checkbox) checkbox.checked = false;
  if (confirmBtn) {
    confirmBtn.disabled = true;
    confirmBtn.innerHTML = `<i data-lucide="check-circle-2" class="w-4 h-4"></i><span>Confirm &amp; Issue Invoice</span>`;
  }

  modal.classList.remove('hidden');
  if (window.lucide) lucide.createIcons();
}

function closeFinalValidationModal() {
  if (isSubmittingFinalOrder) return;
  const modal = document.getElementById('posFinalValidationModal');
  if (modal) modal.classList.add('hidden');
  pendingOrderSubmission = null;
}

function toggleFinalConfirmButton() {
  const checkbox = document.getElementById('valConfirmCheckbox');
  const confirmBtn = document.getElementById('btnValConfirmSubmit');
  if (confirmBtn) {
    confirmBtn.disabled = !(checkbox && checkbox.checked) || isSubmittingFinalOrder;
  }
}

async function confirmAndSubmitPendingOrder() {
  if (!pendingOrderSubmission || isSubmittingFinalOrder) return;
  const config = pendingOrderSubmission;
  isSubmittingFinalOrder = true;

  const confirmBtn = document.getElementById('btnValConfirmSubmit');
  const cancelBtn = document.getElementById('btnValCancel');
  if (confirmBtn) {
    confirmBtn.disabled = true;
    confirmBtn.innerHTML = `<span class="animate-spin mr-1">⟳</span> Recording Order...`;
  }
  if (cancelBtn) cancelBtn.disabled = true;

  try {
    const res = await fetch('/api/pos/checkout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config.payload)
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.message || 'Server error recording order');
    }

    // Close modal before alerting
    isSubmittingFinalOrder = false;
    closeFinalValidationModal();

    const p = config.display;
    const label = config.orderType === 'Custom' ? 'Custom Invoice' : 'Invoice';
    alert(`Success! ${label} ${data.invoiceNo} successfully generated & saved to Firebase.\n\nDeceased: ${p.deceasedName}\nContact: ${p.contactPerson} (${p.contactNumber})\nTotal: ${formatPHP(p.totalAmount)}\nDown Payment: ${formatPHP(p.downPayment)}\nRemaining Balance: ${formatPHP(p.remainingBalance)}`);

    if (config.orderType === 'Custom') {
      resetCustomOrderForm();
      showNotice(`Custom Invoice ${data.invoiceNo} created! View in Billing & Invoices under Custom Orders.`, 'success');
    } else {
      clearCurrentScan();
      showNotice(`Invoice ${data.invoiceNo} created! View in Billing & Invoices.`, 'success');
    }
  } catch (err) {
    alert('Error processing transaction: ' + err.message);
  } finally {
    isSubmittingFinalOrder = false;
    if (cancelBtn) cancelBtn.disabled = false;
    toggleFinalConfirmButton();
    if (confirmBtn) {
      confirmBtn.innerHTML = `<i data-lucide="check-circle-2" class="w-4 h-4"></i><span>Confirm &amp; Issue Invoice</span>`;
      if (window.lucide) lucide.createIcons();
    }
  }
}

async function processOrder() {
  if (!currentScannedItem || !currentScannedItem.casketCode) {
    alert('Please scan or select a funeral package first before issuing an invoice.');
    return;
  }

  if (typeof currentScannedItem.stock === 'number' && currentScannedItem.stock <= 0) {
    alert(`Cannot proceed: "${currentScannedItem.name}" is currently Out of Stock.`);
    return;
  }

  const rawDeceased = document.getElementById('deceasedName')?.value || '';
  const rawContact = document.getElementById('contactPerson')?.value || '';
  const rawMobile = document.getElementById('contactNumber')?.value || '';

  const selectedAddOns = [];
  document.querySelectorAll('#addOnsList .addon-checkbox:checked').forEach(cb => {
    selectedAddOns.push({
      name: cb.getAttribute('data-name'),
      price: parseFloat(cb.getAttribute('data-price')) || 0
    });
  });

  const basePrice = currentScannedItem ? (currentScannedItem.basePrice || 0) : 0;
  const addOnsTotal = selectedAddOns.reduce((sum, a) => sum + a.price, 0);
  const subtotal = basePrice + addOnsTotal;

  const customDiscInput = document.getElementById('customDiscountInput');
  const discountPercent = customDiscInput ? parseFloat(customDiscInput.value || '0') : 0;
  const discountAmount = Math.max(0, (subtotal * discountPercent) / 100);
  const totalAmount = Math.max(0, subtotal - discountAmount);

  const dpInput = document.getElementById('downPaymentInput');
  const downPayment = dpInput ? parseFloat(dpInput.value || '0') : 0;
  const remainingBalance = Math.max(0, totalAmount - downPayment);
  const paymentMethod = document.getElementById('paymentMethodSelect') ? document.getElementById('paymentMethodSelect').value : 'Cash';

  const validated = validateOrderFields({
    deceasedName: rawDeceased,
    contactPerson: rawContact,
    contactNumber: rawMobile,
    discountPercent,
    totalAmount,
    downPayment,
    ids: {
      deceased: 'deceasedName',
      contact: 'contactPerson',
      mobile: 'contactNumber',
      discount: 'customDiscountInput',
      downPayment: 'downPaymentInput'
    }
  });
  if (!validated) return;

  const payload = {
    orderType: 'Package',
    scannedPackage: {
      code: currentScannedItem.casketCode,
      name: currentScannedItem.name,
      basePrice: basePrice
    },
    addOns: selectedAddOns,
    customer: {
      deceasedName: validated.deceasedName,
      contactPerson: validated.contactPerson,
      contactNumber: validated.contactNumber
    },
    pricing: {
      basePrice,
      addOnsTotal,
      subtotal,
      seniorDiscountApplied: discountPercent > 0,
      discountAmount,
      totalAmount,
      downPayment,
      remainingBalance,
      paymentMethod
    }
  };

  openFinalValidationModal({
    orderType: 'Package',
    payload,
    warnings: validated.warnings,
    display: {
      packageName: `${currentScannedItem.name} (${currentScannedItem.casketCode})`,
      addOns: selectedAddOns,
      deceasedName: validated.deceasedName,
      contactPerson: validated.contactPerson,
      contactNumber: validated.contactNumber,
      subtotal,
      discountPercent,
      discountAmount,
      totalAmount,
      downPayment,
      remainingBalance,
      paymentMethod
    }
  });
}

function resetPOSForm() {
  if (document.getElementById('deceasedName')) document.getElementById('deceasedName').value = '';
  if (document.getElementById('contactPerson')) document.getElementById('contactPerson').value = '';
  if (document.getElementById('contactNumber')) document.getElementById('contactNumber').value = '';
  if (document.getElementById('customDiscountInput')) document.getElementById('customDiscountInput').value = '0';
  document.querySelectorAll('#addOnsList .addon-checkbox').forEach(cb => cb.checked = false);
  setDownPaymentPreset('50');
}


// =====================================================================
// SECTION 2: CUSTOM ORDER BUILDER LOGIC (Client Picks Flowers, Casket, Lights, etc.)
// =====================================================================
let customDpMode = '50'; // '50', '100', 'custom'

let customOrderState = {
  casket: { name: 'None', price: 0, code: 'NONE' },
  flowers: { name: 'None', price: 0 },
  lights: { name: 'None', price: 0 },
  chapel: { name: 'None', price: 0 },
  embalming: { name: 'None', price: 0 },
  hearse: { name: 'None', price: 0 },
  addOns: [],
  subtotal: 0,
  discountPercent: 0,
  discountAmount: 0,
  totalAmount: 0,
  downPayment: 0,
  remainingBalance: 0
};

function handleCustomSelectionChange() {
  // 1. Casket Choice
  const casketRadio = document.querySelector('input[name="customCasketChoice"]:checked');
  const boxCustomCasket = document.getElementById('boxCustomCasketInput');
  let casketName = "None";
  let casketPrice = 0;
  let casketCode = "NONE";

  if (casketRadio) {
    if (casketRadio.value === 'CUSTOM') {
      if (boxCustomCasket) boxCustomCasket.classList.remove('hidden');
      casketName = (document.getElementById('customCasketNameInput')?.value || 'Custom Tailored Casket').trim();
      casketPrice = parseFloat(document.getElementById('customCasketPriceInput')?.value) || 0;
      casketCode = 'CUSTOM';
    } else {
      if (boxCustomCasket) boxCustomCasket.classList.add('hidden');
      casketName = casketRadio.getAttribute('data-name') || 'Casket';
      casketPrice = parseFloat(casketRadio.getAttribute('data-price')) || 0;
      casketCode = casketRadio.getAttribute('data-code') || 'CSK-CUSTOM';
    }
  }
  customOrderState.casket = { name: casketName, price: casketPrice, code: casketCode };

  // 2. Floral Arrangements (Flowers)
  const flowerRadio = document.querySelector('input[name="customFlowerChoice"]:checked');
  const boxCustomFlower = document.getElementById('boxCustomFlowerInput');
  let flowerName = "None";
  let flowerPrice = 0;

  if (flowerRadio) {
    if (flowerRadio.value === 'CUSTOM') {
      if (boxCustomFlower) boxCustomFlower.classList.remove('hidden');
      flowerName = (document.getElementById('customFlowerNameInput')?.value || 'Custom Flower Package').trim();
      flowerPrice = parseFloat(document.getElementById('customFlowerPriceInput')?.value) || 0;
    } else {
      if (boxCustomFlower) boxCustomFlower.classList.add('hidden');
      flowerName = flowerRadio.getAttribute('data-name') || 'Flowers';
      flowerPrice = parseFloat(flowerRadio.getAttribute('data-price')) || 0;
    }
  }
  customOrderState.flowers = { name: flowerName, price: flowerPrice };

  // 3. Vigil Lighting & Ambiance (Lights)
  const lightRadio = document.querySelector('input[name="customLightChoice"]:checked');
  const boxCustomLight = document.getElementById('boxCustomLightInput');
  let lightName = "None";
  let lightPrice = 0;

  if (lightRadio) {
    if (lightRadio.value === 'CUSTOM') {
      if (boxCustomLight) boxCustomLight.classList.remove('hidden');
      lightName = (document.getElementById('customLightNameInput')?.value || 'Custom Vigil Lighting').trim();
      lightPrice = parseFloat(document.getElementById('customLightPriceInput')?.value) || 0;
    } else {
      if (boxCustomLight) boxCustomLight.classList.add('hidden');
      lightName = lightRadio.getAttribute('data-name') || 'Lighting';
      lightPrice = parseFloat(lightRadio.getAttribute('data-price')) || 0;
    }
  }
  customOrderState.lights = { name: lightName, price: lightPrice };

  // 4. Chapel / Venue Choice
  const chapelRadio = document.querySelector('input[name="customChapelChoice"]:checked');
  const boxCustomChapel = document.getElementById('boxCustomChapelInput');
  let chapelName = "None";
  let chapelPrice = 0;

  if (chapelRadio) {
    if (chapelRadio.value === 'CUSTOM') {
      if (boxCustomChapel) boxCustomChapel.classList.remove('hidden');
      chapelName = (document.getElementById('customChapelNameInput')?.value || 'Custom Venue Setup').trim();
      chapelPrice = parseFloat(document.getElementById('customChapelPriceInput')?.value) || 0;
    } else {
      if (boxCustomChapel) boxCustomChapel.classList.add('hidden');
      chapelName = chapelRadio.getAttribute('data-name') || 'Chapel / Venue';
      chapelPrice = parseFloat(chapelRadio.getAttribute('data-price')) || 0;
    }
  }
  customOrderState.chapel = { name: chapelName, price: chapelPrice };

  // 5. Embalming & Care Choice
  const embalmRadio = document.querySelector('input[name="customEmbalmingChoice"]:checked');
  const boxCustomEmbalm = document.getElementById('boxCustomEmbalmInput');
  let embalmName = "None";
  let embalmPrice = 0;

  if (embalmRadio) {
    if (embalmRadio.value === 'CUSTOM') {
      if (boxCustomEmbalm) boxCustomEmbalm.classList.remove('hidden');
      embalmName = (document.getElementById('customEmbalmNameInput')?.value || 'Custom Preservation Care').trim();
      embalmPrice = parseFloat(document.getElementById('customEmbalmPriceInput')?.value) || 0;
    } else {
      if (boxCustomEmbalm) boxCustomEmbalm.classList.add('hidden');
      embalmName = embalmRadio.getAttribute('data-name') || 'Embalming';
      embalmPrice = parseFloat(embalmRadio.getAttribute('data-price')) || 0;
    }
  }
  customOrderState.embalming = { name: embalmName, price: embalmPrice };

  // 6. Hearse Choice
  const hearseRadio = document.querySelector('input[name="customHearseChoice"]:checked');
  let hearseName = "None";
  let hearsePrice = 0;

  if (hearseRadio) {
    hearseName = hearseRadio.getAttribute('data-name') || 'Hearse';
    hearsePrice = parseFloat(hearseRadio.getAttribute('data-price')) || 0;
  }
  customOrderState.hearse = { name: hearseName, price: hearsePrice };

  // 7. Add-ons
  const selectedCustomAddOns = [];
  let addOnsTotal = 0;
  document.querySelectorAll('#customAddOnsList .custom-addon-checkbox:checked').forEach(cb => {
    const price = parseFloat(cb.getAttribute('data-price')) || 0;
    const name = cb.getAttribute('data-name') || 'Extra Service';
    selectedCustomAddOns.push({ name, price });
    addOnsTotal += price;
  });
  customOrderState.addOns = selectedCustomAddOns;

  // Calculate Subtotal
  const subtotal = casketPrice + flowerPrice + lightPrice + chapelPrice + embalmPrice + hearsePrice + addOnsTotal;
  customOrderState.subtotal = subtotal;

  // Discount
  const discInput = document.getElementById('customDiscountPercent');
  const discountPercent = discInput ? (parseFloat(discInput.value) || 0) : 0;
  const discountAmount = Math.max(0, (subtotal * discountPercent) / 100);
  const totalAmount = Math.max(0, subtotal - discountAmount);
  customOrderState.discountPercent = discountPercent;
  customOrderState.discountAmount = discountAmount;
  customOrderState.totalAmount = totalAmount;

  // Down Payment computation
  const dpInput = document.getElementById('customDownPaymentInput');
  let downPayment = 0;

  if (customDpMode === '50') {
    downPayment = Math.round(totalAmount * 0.50);
    if (dpInput) dpInput.value = downPayment;
  } else if (customDpMode === '100') {
    downPayment = totalAmount;
    if (dpInput) dpInput.value = downPayment;
  } else {
    const rawVal = dpInput ? String(dpInput.value).trim() : '0';
    if (rawVal.includes('-')) {
      downPayment = 0;
    } else {
      downPayment = dpInput ? (parseFloat(dpInput.value) || 0) : 0;
      if (Object.is(downPayment, -0)) downPayment = 0;
    }
  }
  const remainingBalance = Math.max(0, totalAmount - downPayment);
  customOrderState.downPayment = downPayment;
  customOrderState.remainingBalance = remainingBalance;

  // Update Right Column DOM
  const lblCasket = document.getElementById('lblCustomCasket');
  const costCasket = document.getElementById('costCustomCasket');
  if (lblCasket) lblCasket.textContent = `⚰️ Casket: ${casketName}`;
  if (costCasket) costCasket.textContent = formatPHP(casketPrice);

  const lblFlower = document.getElementById('lblCustomFlower');
  const costFlower = document.getElementById('costCustomFlower');
  if (lblFlower) lblFlower.textContent = `🌸 Flowers: ${flowerName}`;
  if (costFlower) costFlower.textContent = formatPHP(flowerPrice);

  const lblLight = document.getElementById('lblCustomLight');
  const costLight = document.getElementById('costCustomLight');
  if (lblLight) lblLight.textContent = `💡 Lights: ${lightName}`;
  if (costLight) costLight.textContent = formatPHP(lightPrice);

  const lblChapel = document.getElementById('lblCustomChapel');
  const costChapel = document.getElementById('costCustomChapel');
  if (lblChapel) lblChapel.textContent = `🏛️ Venue: ${chapelName}`;
  if (costChapel) costChapel.textContent = formatPHP(chapelPrice);

  const lblEmbalm = document.getElementById('lblCustomEmbalm');
  const costEmbalm = document.getElementById('costCustomEmbalm');
  if (lblEmbalm) lblEmbalm.textContent = `🕊️ Embalming: ${embalmName}`;
  if (costEmbalm) costEmbalm.textContent = formatPHP(embalmPrice);

  const lblHearse = document.getElementById('lblCustomHearse');
  const costHearse = document.getElementById('costCustomHearse');
  if (lblHearse) lblHearse.textContent = `🚗 Hearse: ${hearseName}`;
  if (costHearse) costHearse.textContent = formatPHP(hearsePrice);

  const costAddons = document.getElementById('costCustomAddOns');
  if (costAddons) costAddons.textContent = `+${formatPHP(addOnsTotal)}`;

  const subtotalDisplay = document.getElementById('customSubtotalDisplay');
  if (subtotalDisplay) subtotalDisplay.textContent = formatPHP(subtotal);

  const heroTotal = document.getElementById('customHeroTotal');
  if (heroTotal) heroTotal.textContent = formatPHP(totalAmount);

  const discountDisplay = document.getElementById('customDiscountAmountDisplay');
  if (discountDisplay) {
    discountDisplay.textContent = discountAmount > 0 ? `-${formatPHP(discountAmount)} (${discountPercent}%)` : "₱0";
  }

  const totalDue = document.getElementById('customTotalDue');
  if (totalDue) totalDue.textContent = formatPHP(totalAmount);

  const remainingDisplay = document.getElementById('customRemainingBalanceDisplay');
  if (remainingDisplay) remainingDisplay.textContent = formatPHP(remainingBalance);
}

function setCustomDpPreset(preset) {
  customDpMode = preset;
  const btn50 = document.getElementById('btnCustomDp50');
  const btn100 = document.getElementById('btnCustomDp100');
  const btnManual = document.getElementById('btnCustomDpManual');
  const badge = document.getElementById('customDpBadge');

  const activeClass = "py-1 px-2 rounded-lg text-[11px] font-bold border border-purple-500 bg-purple-50 text-purple-800 transition-all text-center";
  const inactiveClass = "py-1 px-2 rounded-lg text-[11px] font-semibold border border-slate-200 bg-white text-slate-700 hover:bg-slate-50 transition-all text-center";

  if (btn50) btn50.className = preset === '50' ? activeClass : inactiveClass;
  if (btn100) btn100.className = preset === '100' ? activeClass : inactiveClass;
  if (btnManual) btnManual.className = preset === 'custom' ? activeClass : inactiveClass;

  if (badge) {
    if (preset === '50') badge.textContent = "50% Deposit";
    else if (preset === '100') badge.textContent = "100% Full Payment";
    else badge.textContent = "Custom Amount";
  }

  handleCustomSelectionChange();
}

function handleCustomDpManualInput() {
  customDpMode = 'custom';
  const btn50 = document.getElementById('btnCustomDp50');
  const btn100 = document.getElementById('btnCustomDp100');
  const btnManual = document.getElementById('btnCustomDpManual');
  const badge = document.getElementById('customDpBadge');

  const activeClass = "py-1 px-2 rounded-lg text-[11px] font-bold border border-purple-500 bg-purple-50 text-purple-800 transition-all text-center";
  const inactiveClass = "py-1 px-2 rounded-lg text-[11px] font-semibold border border-slate-200 bg-white text-slate-700 hover:bg-slate-50 transition-all text-center";

  if (btn50) btn50.className = inactiveClass;
  if (btn100) btn100.className = inactiveClass;
  if (btnManual) btnManual.className = activeClass;
  if (badge) badge.textContent = "Custom Amount";

  const totalAmount = customOrderState.totalAmount || 0;
  const dpInput = document.getElementById('customDownPaymentInput');
  const rawDpStr = dpInput ? String(dpInput.value).trim() : '0';
  let dp = 0;
  if (!rawDpStr.includes('-')) {
    dp = dpInput ? (parseFloat(dpInput.value) || 0) : 0;
    if (Object.is(dp, -0)) dp = 0;
  }
  const rem = Math.max(0, totalAmount - dp);
  customOrderState.downPayment = dp;
  customOrderState.remainingBalance = rem;

  const remainingDisplay = document.getElementById('customRemainingBalanceDisplay');
  if (remainingDisplay) remainingDisplay.textContent = formatPHP(rem);
}

// Process and Submit Custom Order to Firestore (with Final Validation Modal)
async function processCustomOrder() {
  const rawDeceased = document.getElementById('customDeceasedName')?.value || '';
  const rawContact = document.getElementById('customContactPerson')?.value || '';
  const rawMobile = document.getElementById('customContactNumber')?.value || '';

  const customDiscInput = document.getElementById('customDiscountPercent');
  const discountPercent = customDiscInput ? parseFloat(customDiscInput.value || '0') : (customOrderState.discountPercent || 0);
  const dpInput = document.getElementById('customDownPaymentInput');
  const downPayment = dpInput ? parseFloat(dpInput.value || '0') : (customOrderState.downPayment || 0);
  const totalAmount = customOrderState.totalAmount || 0;
  const remainingBalance = Math.max(0, totalAmount - downPayment);

  const validated = validateOrderFields({
    deceasedName: rawDeceased,
    contactPerson: rawContact,
    contactNumber: rawMobile,
    discountPercent,
    totalAmount,
    downPayment,
    ids: {
      deceased: 'customDeceasedName',
      contact: 'customContactPerson',
      mobile: 'customContactNumber',
      discount: 'customDiscountPercent',
      downPayment: 'customDownPaymentInput'
    }
  });
  if (!validated) return;

  // Construct Custom Name string
  const cName = customOrderState.casket.name || 'Casket';
  const fName = customOrderState.flowers.name || 'Flowers';
  const lName = customOrderState.lights.name || 'Lights';
  const orderTitle = `Custom Funeral Order: ${cName} • ${fName} • ${lName}`;

  const paymentMethod = document.getElementById('customPaymentMethodSelect')?.value || 'Cash';

  const payload = {
    orderType: 'Custom',
    casketCode: customOrderState.casket.code || 'CUSTOM',
    casketName: orderTitle,
    customDetails: {
      casket: customOrderState.casket,
      flowers: customOrderState.flowers,
      lights: customOrderState.lights,
      chapel: customOrderState.chapel,
      embalming: customOrderState.embalming,
      hearse: customOrderState.hearse
    },
    addOns: customOrderState.addOns,
    customer: {
      deceasedName: validated.deceasedName,
      contactPerson: validated.contactPerson,
      contactNumber: validated.contactNumber
    },
    pricing: {
      basePrice: customOrderState.casket.price,
      addOnsTotal: customOrderState.subtotal - customOrderState.casket.price,
      subtotal: customOrderState.subtotal,
      seniorDiscountApplied: discountPercent > 0,
      discountAmount: customOrderState.discountAmount,
      totalAmount: totalAmount,
      downPayment: downPayment,
      remainingBalance: remainingBalance,
      paymentMethod: paymentMethod
    }
  };

  openFinalValidationModal({
    orderType: 'Custom',
    payload,
    warnings: validated.warnings,
    display: {
      packageName: orderTitle,
      addOns: customOrderState.addOns || [],
      deceasedName: validated.deceasedName,
      contactPerson: validated.contactPerson,
      contactNumber: validated.contactNumber,
      subtotal: customOrderState.subtotal,
      discountPercent,
      discountAmount: customOrderState.discountAmount,
      totalAmount,
      downPayment,
      remainingBalance,
      paymentMethod
    }
  });
}

function resetCustomOrderForm() {
  if (document.getElementById('customDeceasedName')) document.getElementById('customDeceasedName').value = '';
  if (document.getElementById('customContactPerson')) document.getElementById('customContactPerson').value = '';
  if (document.getElementById('customContactNumber')) document.getElementById('customContactNumber').value = '';
  if (document.getElementById('customDiscountPercent')) document.getElementById('customDiscountPercent').value = '0';
  document.querySelectorAll('#customAddOnsList .custom-addon-checkbox').forEach(cb => cb.checked = false);

  // Reset radios to defaults
  const firstCasket = document.querySelector('input[name="customCasketChoice"]');
  if (firstCasket) firstCasket.checked = true;

  const defaultFlower = document.querySelector('input[name="customFlowerChoice"][value="NONE"]');
  if (defaultFlower) defaultFlower.checked = true;

  const defaultLight = document.querySelector('input[name="customLightChoice"][value="NONE"]');
  if (defaultLight) defaultLight.checked = true;

  const defaultChapel = document.querySelector('input[name="customChapelChoice"][value="HOME_3"]');
  if (defaultChapel) defaultChapel.checked = true;

  const defaultEmbalm = document.querySelector('input[name="customEmbalmingChoice"][value="STD"]');
  if (defaultEmbalm) defaultEmbalm.checked = true;

  const defaultHearse = document.querySelector('input[name="customHearseChoice"][value="STD"]');
  if (defaultHearse) defaultHearse.checked = true;

  setCustomDpPreset('50');
}

// Display Notice Banner
function showNotice(msg, type) {
  const banner = document.getElementById('statusNotice');
  if (!banner) return;
  banner.classList.remove('hidden', 'bg-emerald-50', 'border-emerald-200', 'text-emerald-800', 'bg-rose-50', 'border-rose-200', 'text-rose-800', 'bg-amber-50', 'border-amber-200', 'text-amber-800');

  if (type === 'success') {
    banner.classList.add('flex', 'bg-emerald-50', 'border-emerald-200', 'text-emerald-800');
  } else if (type === 'error') {
    banner.classList.add('flex', 'bg-rose-50', 'border-rose-200', 'text-rose-800');
  } else {
    banner.classList.add('flex', 'bg-amber-50', 'border-amber-200', 'text-amber-800');
  }

  banner.innerHTML = `
    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
    <span>${msg}</span>
  `;

  setTimeout(() => {
    banner.classList.add('hidden');
    banner.classList.remove('flex');
  }, 5000);
}

// =====================================================================
// SECTION 4: ESP32 QR PROJECTION & SHOWCASE CONTROLLER
// =====================================================================
let activeModalCasketCode = null;
let activeModalCasketName = null;

async function projectQrToEsp32(casketCode, packageName) {
  if (!casketCode) return;
  
  const btn = document.getElementById(`btnProject_${casketCode}`);
  const originalHtml = btn ? btn.innerHTML : '';
  
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<i data-lucide="loader-2" class="w-3.5 h-3.5 animate-spin text-yellow-400"></i><span>Sending...</span>`;
    if (window.lucide) lucide.createIcons();
  }

  showNotice(`Sending QR for "${packageName || casketCode}" to ESP32 projector...`, 'info');

  try {
    const res = await fetch('/api/project-qr', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ casket_code: casketCode })
    });
    const result = await res.json();

    if (result.status === 'success') {
      showNotice(`✓ Success: ${result.message}`, 'success');
      if (typeof showToast === 'function') {
        showToast(result.message, 'success');
      }
    } else if (result.status === 'offline' || result.offline) {
      showNotice(`⚠️ ESP32 Device Notice: ${result.message}`, 'warning');
      if (typeof showToast === 'function') {
        showToast(`ESP32 Projector is offline at ${result.esp32_ip || 'configured IP'}. Please check device Wi-Fi/power.`, 'warning');
      }
    } else {
      showNotice(result.message || 'Failed to project QR to ESP32.', 'error');
      if (typeof showToast === 'function') {
        showToast(result.message || 'Failed to project QR to ESP32.', 'error');
      }
    }
  } catch (err) {
    console.error('projectQrToEsp32 error:', err);
    showNotice(`Network error while projecting QR to ESP32: ${err.message}`, 'error');
    if (typeof showToast === 'function') {
      showToast('Network error while connecting to projection service.', 'error');
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = originalHtml;
      if (window.lucide) lucide.createIcons();
    }
  }
}

function loadPackageDirectly(casketCode) {
  if (!casketCode) return;
  scanPackageCode(casketCode);
  const target = document.getElementById('scannedPackageContainer') || document.getElementById('viewPackageOrder');
  if (target) {
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function openPosQrPreview(code, name, price, qrUrl) {
  activeModalCasketCode = code;
  activeModalCasketName = name;
  const nameEl = document.getElementById('posModalPkgName');
  const codeEl = document.getElementById('posModalPkgCode');
  const priceEl = document.getElementById('posModalPkgPrice');
  const imgEl = document.getElementById('posModalQrImg');
  const modal = document.getElementById('posQrModal');

  if (nameEl) nameEl.textContent = name || code;
  if (codeEl) codeEl.textContent = code;
  if (priceEl) priceEl.textContent = price || '₱0';
  if (imgEl) imgEl.src = qrUrl || `/static/uploads/qrcodes/${code}.png`;
  if (modal) modal.classList.remove('hidden');
  if (window.lucide) lucide.createIcons();
}

function closePosQrPreview() {
  const modal = document.getElementById('posQrModal');
  if (modal) modal.classList.add('hidden');
}

function projectActiveModalQr() {
  if (activeModalCasketCode) {
    projectQrToEsp32(activeModalCasketCode, activeModalCasketName);
  }
}

// Initialize on page load
window.addEventListener('DOMContentLoaded', () => {
  // Start with empty Awaiting QR Scan state (no fake simulated package)
  renderScannedItem(null);
  setDownPaymentPreset('50');

  // Activate ESP32 hardware listener auto-polling
  toggleAutoPolling(true);
  fetchLatestScan(false);

  // Select first casket radio if none checked
  const checkedCasket = document.querySelector('input[name="customCasketChoice"]:checked');
  if (!checkedCasket) {
    const firstCasket = document.querySelector('input[name="customCasketChoice"]');
    if (firstCasket) firstCasket.checked = true;
  }
  handleCustomSelectionChange();
});
