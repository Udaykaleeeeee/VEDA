/* Mobile capture controller: durable media + confirmed text + offline outbox. */
(() => {
  const DB = 'veda-field-sync';
  const STORE = 'outbox';
  const state = { files: [], recording: null, chunks: [], recognition: null,
    activity: null, coordinates: null, transcriptDirty: false, timer: null,
    extracted: false, projectId: null };

  const el = (id) => document.getElementById(id);
  const escapeHtml = (value) => window.esc(value);
  const clientId = () => 'fc_' + (crypto.randomUUID ? crypto.randomUUID() :
    Date.now().toString(36) + Math.random().toString(36).slice(2));

  function openDb() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB, 1);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(STORE)) {
          request.result.createObjectStore(STORE, {keyPath: 'id'});
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function allQueued() {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const request = db.transaction(STORE).objectStore(STORE).getAll();
      request.onsuccess = () => resolve(request.result || []);
      request.onerror = () => reject(request.error);
    });
  }

  async function queue(item) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).put(item);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  }

  async function remove(id) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).delete(id);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  }

  function formFor(item) {
    const form = new FormData();
    form.append('payload', JSON.stringify(item.payload));
    for (const file of item.attachments || []) {
      form.append('files', file.blob, file.name || 'field-media');
    }
    return form;
  }

  async function send(item) {
    const response = await fetch('/api/projects/' + encodeURIComponent(item.projectId) +
      '/field-captures', {method: 'POST', body: formFor(item)});
    if (!response.ok) {
      let message = 'Capture could not be saved';
      try { message = (await response.json()).detail || message; } catch (_) {}
      const error = new Error(message); error.permanent = response.status < 500;
      throw error;
    }
    return response.json();
  }

  async function flush(projectId) {
    if (!navigator.onLine) return;
    for (const item of (await allQueued()).filter(x => !projectId || x.projectId === projectId)) {
      try { await send({...item, payload: {...item.payload, sync_source: 'offline_outbox'}}); await remove(item.id); }
      catch (error) { if (!error.permanent) break; }
    }
    await updateOutbox(projectId);
  }

  async function updateOutbox(projectId) {
    const items = (await allQueued()).filter(x => !projectId || x.projectId === projectId);
    const count = el('capture-outbox-count');
    if (count) count.textContent = String(items.length);
    const status = el('capture-connectivity');
    if (status) {
      status.className = 'capture-connectivity ' + (navigator.onLine ? 'online' : 'offline');
      status.textContent = navigator.onLine ? (items.length ? 'Online · syncing ' + items.length : 'Online · synced')
        : 'Offline · ' + items.length + ' saved on device';
    }
  }

  function drawFiles() {
    const tray = el('capture-media-tray');
    if (!tray) return;
    tray.innerHTML = state.files.length ? state.files.map((file, index) =>
      '<div class="capture-media-item"><span>' + escapeHtml(file.name) +
      '</span><small>' + Math.max(1, Math.round(file.blob.size / 1024)) +
      ' KB</small><button type="button" data-capture-remove="' + index +
      '" aria-label="Remove ' + escapeHtml(file.name) + '">×</button></div>').join('') :
      '<div class="capture-media-empty">Photos and voice recordings will appear here.</div>';
    tray.querySelectorAll('[data-capture-remove]').forEach(button => button.onclick = () => {
      state.files.splice(Number(button.dataset.captureRemove), 1); drawFiles();
    });
  }

  function addInputFiles(files) {
    for (const file of Array.from(files || [])) {
      state.files.push({name: file.name, type: file.type, blob: file});
    }
    drawFiles();
  }

  function eventState() {
    return (document.querySelector('[name="capture-event"]:checked') || {}).value || 'progress';
  }

  function updateEventFields() {
    const value = eventState();
    document.querySelectorAll('.capture-event-option').forEach(option =>
      option.classList.toggle('selected', option.querySelector('input').checked));
    if (el('capture-progress-fields')) el('capture-progress-fields').hidden = value !== 'progress';
    if (el('capture-finish-rule')) el('capture-finish-rule').hidden = value !== 'finish';
  }

  function setTranscript(text, source) {
    const original = el('capture-original');
    const confirmed = el('capture-confirmed');
    if (original) original.value = text;
    if (confirmed && !state.transcriptDirty) confirmed.value = text;
    const hint = el('capture-transcript-source');
    if (hint) hint.textContent = source;
  }

  async function toggleRecording() {
    const button = el('capture-voice');
    if (state.recording && state.recording.state === 'recording') {
      state.recording.stop();
      if (state.recognition) { try { state.recognition.stop(); } catch (_) {} }
      button.classList.remove('recording'); button.textContent = 'Record voice';
      return;
    }
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      el('capture-audio-file').click(); return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio: true});
      state.chunks = [];
      state.recording = new MediaRecorder(stream);
      state.recording.ondataavailable = (event) => { if (event.data.size) state.chunks.push(event.data); };
      state.recording.onstop = () => {
        const type = state.recording.mimeType || 'audio/webm';
        const blob = new Blob(state.chunks, {type});
        state.files.push({name: 'field-voice-' + Date.now() + '.webm', type, blob});
        stream.getTracks().forEach(track => track.stop()); drawFiles();
        setTimeout(() => interpretEvent(state.projectId).catch(error =>
          window.toast('Could not extract the event card: ' + error.message, 'bad')), 250);
      };
      state.recording.start(500);
      button.classList.add('recording'); button.textContent = 'Stop recording';
      const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (Recognition) {
        const recognition = new Recognition();
        recognition.lang = el('capture-language').value;
        recognition.continuous = true; recognition.interimResults = true;
        let finalText = el('capture-original').value.trim();
        recognition.onresult = (event) => {
          let interim = '';
          for (let i = event.resultIndex; i < event.results.length; i += 1) {
            if (event.results[i].isFinal) finalText += (finalText ? ' ' : '') + event.results[i][0].transcript;
            else interim += event.results[i][0].transcript;
          }
          setTranscript((finalText + (interim ? ' ' + interim : '')).trim(),
            'Live transcript is a draft—confirm it below. The recording remains the source.');
        };
        recognition.start(); state.recognition = recognition;
      } else {
        el('capture-transcript-source').textContent =
          'Voice is recorded as evidence. Type or paste the words to confirm the update.';
      }
    } catch (error) { window.toast('Microphone unavailable: ' + error.message, 'bad'); }
  }

  async function locate() {
    if (!navigator.geolocation) return window.toast('Location is not supported on this device', 'bad');
    const button = el('capture-location'); button.disabled = true; button.textContent = 'Locating…';
    navigator.geolocation.getCurrentPosition((position) => {
      state.coordinates = {latitude: position.coords.latitude,
        longitude: position.coords.longitude,
        location_accuracy_m: position.coords.accuracy, location_source: 'device'};
      el('capture-location-status').textContent = 'Device location attached · ±' +
        Math.round(position.coords.accuracy) + ' m';
      button.disabled = false; button.textContent = 'Refresh location';
    }, (error) => {
      button.disabled = false; button.textContent = 'Use device location';
      window.toast('Location not attached: ' + error.message, 'bad');
    }, {enableHighAccuracy: true, timeout: 12000, maximumAge: 30000});
  }

  async function searchActivities(projectId, query) {
    const list = el('capture-activity-results');
    if (!query.trim()) { list.innerHTML = ''; return; }
    const response = await window.api('/projects/' + projectId + '/activities?q=' +
      encodeURIComponent(query.trim()) + '&milestone=0&limit=8');
    renderActivityCandidates(response.activities || []);
  }

  function selectActivity(activity) {
    state.activity = activity;
    el('capture-activity-search').value = (activity.display_id || '') + ' · ' + activity.name;
    el('capture-activity-results').innerHTML = '';
    el('capture-activity-selected').innerHTML = '<b>Linked to ' +
      escapeHtml(activity.display_id || 'UID ' + activity.uid) + '</b><span>' +
      escapeHtml(activity.name) + '</span><button type="button" id="capture-activity-clear">Change</button>';
    el('capture-activity-clear').onclick = () => { state.activity = null;
      el('capture-activity-search').value = ''; el('capture-activity-selected').innerHTML = ''; };
  }

  function renderActivityCandidates(activities) {
    const list = el('capture-activity-results');
    list.innerHTML = activities.length ? activities.map(activity =>
      '<button type="button" data-capture-activity="' + activity.uid + '">' +
      '<b>' + escapeHtml(activity.display_id || 'UID ' + activity.uid) + '</b>' +
      '<span>' + escapeHtml(activity.name) + '</span><small>' +
      escapeHtml(activity.wbs || '') + ' · ' + escapeHtml(activity.status || '—') +
      '</small></button>').join('') : '<div class="capture-no-result">No matching activities.</div>';
    list.querySelectorAll('[data-capture-activity]').forEach((button, index) =>
      button.onclick = () => selectActivity(activities[index]));
  }

  async function interpretEvent(projectId) {
    const text = el('capture-original').value.trim();
    if (!text) return window.toast('Speak or type the field observation first', 'bad');
    const button = el('capture-extract'); button.disabled = true; button.textContent = 'Extracting event…';
    let result;
    if (navigator.onLine) {
      const response = await fetch('/api/projects/' + encodeURIComponent(projectId) +
        '/field-captures/interpret', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({text, occurred_at: el('capture-occurred').value,
            location_label: el('capture-location-label').value})});
      if (!response.ok) {
        let message = 'Event extraction failed';
        try { message = (await response.json()).detail || message; } catch (_) {}
        button.disabled = false; button.textContent = 'Extract editable event card';
        throw new Error(message);
      }
      result = await response.json();
    } else {
      const pct = text.match(/\b(100(?:\.0+)?|\d{1,2}(?:\.\d+)?)\s*%/);
      result = {draft: {event_state: /finish|complete/i.test(text) ? 'finish' :
        /start|commenc/i.test(text) ? 'start' : 'progress',
        observed_progress: pct ? Number(pct[1]) : null, confidence: null,
        asset_tags: []}, activity_candidates: []};
    }
    const draft = result.draft || {};
    const radio = document.querySelector('[name="capture-event"][value="' +
      (draft.event_state || 'progress') + '"]');
    if (radio) radio.checked = true;
    if (draft.observed_progress !== null && draft.observed_progress !== undefined)
      el('capture-progress').value = draft.observed_progress;
    if (draft.remaining_days !== null && draft.remaining_days !== undefined)
      el('capture-remaining').value = draft.remaining_days;
    if (draft.location_label && !el('capture-location-label').value)
      el('capture-location-label').value = draft.location_label;
    updateEventFields();
    renderActivityCandidates(result.activity_candidates || []);
    const summary = [];
    if (draft.action) summary.push('<span>Action · ' + escapeHtml(draft.action) + '</span>');
    summary.push('<span>Event · ' + escapeHtml(draft.event_state || 'progress') + '</span>');
    if (draft.quantity !== null && draft.quantity !== undefined)
      summary.push('<span>Quantity · ' + escapeHtml(draft.quantity + ' ' + (draft.unit || '')) + '</span>');
    if ((draft.asset_tags || []).length)
      summary.push('<span>Assets · ' + escapeHtml(draft.asset_tags.join(', ')) + '</span>');
    summary.push('<span>Draft only · edit before confirming</span>');
    el('capture-extracted-summary').innerHTML = summary.join('');
    for (const id of ['capture-structured-card', 'capture-place-card', 'capture-confirm-card'])
      el(id).hidden = false;
    if (!state.transcriptDirty) el('capture-confirmed').value = text;
    state.extracted = true;
    button.disabled = false; button.textContent = 'Re-extract event card';
  }

  function capturePayload() {
    const occurred = el('capture-occurred').value;
    let occurredWithZone = '';
    if (occurred) {
      const offsetMinutes = -new Date(occurred).getTimezoneOffset();
      const sign = offsetMinutes >= 0 ? '+' : '-';
      const absolute = Math.abs(offsetMinutes);
      occurredWithZone = occurred + sign + String(Math.floor(absolute / 60)).padStart(2, '0') +
        ':' + String(absolute % 60).padStart(2, '0');
    }
    return {
      client_capture_id: el('capture-client-id').value,
      occurred_at: occurredWithZone,
      event_state: eventState(), language: el('capture-language').value,
      reporter: el('capture-reporter').value.trim() || 'Field reporter',
      original_text: el('capture-original').value.trim(),
      confirmed_text: el('capture-confirmed').value.trim(),
      activity_uid: state.activity ? state.activity.uid : null,
      observed_progress: eventState() === 'progress' && el('capture-progress').value !== ''
        ? Number(el('capture-progress').value) : null,
      remaining_days: eventState() === 'progress' && el('capture-remaining').value !== ''
        ? Number(el('capture-remaining').value) : (eventState() === 'finish' ? 0 : null),
      location_label: el('capture-location-label').value.trim(),
      ...(state.coordinates || {}), sync_source: navigator.onLine ? 'online' : 'offline_outbox'
    };
  }

  function reset() {
    state.files = []; state.activity = null; state.coordinates = null;
    state.transcriptDirty = false; state.extracted = false;
    el('capture-client-id').value = clientId();
    el('capture-original').value = ''; el('capture-confirmed').value = '';
    el('capture-progress').value = ''; el('capture-remaining').value = '';
    el('capture-activity-search').value = ''; el('capture-activity-selected').innerHTML = '';
    el('capture-location-label').value = ''; el('capture-location-status').textContent = 'Location is optional and permission-based.';
    el('capture-extracted-summary').innerHTML = '';
    for (const id of ['capture-structured-card', 'capture-place-card', 'capture-confirm-card'])
      el(id).hidden = true;
    el('capture-extract').textContent = 'Extract editable event card';
    drawFiles();
  }

  async function submit(projectId) {
    const payload = capturePayload();
    if (!state.extracted) return window.toast('Extract and review the editable event card first', 'bad');
    if (!payload.confirmed_text) return window.toast('Confirm the field update text first', 'bad');
    const item = {id: payload.client_capture_id, projectId, payload,
      attachments: state.files.slice(), createdAt: Date.now()};
    const button = el('capture-save'); button.disabled = true;
    button.textContent = navigator.onLine ? 'Saving confirmed update…' : 'Saving on this device…';
    if (navigator.onLine) {
      try {
        const result = await send(item);
        const capture = result.capture || {};
        window.toast(capture.status === 'proposal_ready'
          ? 'Saved. Governed actuals proposals are ready for planner review.'
          : capture.status === 'needs_activity'
            ? 'Saved. A planner still needs to link this update to an activity.'
            : 'Confirmed field update saved.', 'good');
        reset(); window.refreshCounts(); window.render(); return;
      } catch (error) {
        if (error.permanent) { window.toast(error.message, 'bad'); button.disabled = false;
          button.textContent = 'Confirm & save update'; return; }
      }
    }
    await queue(item);
    if ('serviceWorker' in navigator) {
      const registration = await navigator.serviceWorker.ready;
      if (registration.sync) { try { await registration.sync.register('veda-field-captures'); } catch (_) {} }
    }
    window.toast('Saved safely on this device. VEDA will sync it when online.', 'good');
    reset(); await updateOutbox(projectId); button.disabled = false; button.textContent = 'Confirm & save update';
  }

  async function bind(projectId) {
    state.files = []; state.activity = null; state.coordinates = null;
    state.transcriptDirty = false; state.extracted = false; state.projectId = projectId;
    el('capture-client-id').value = clientId();
    const now = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);
    if (!el('capture-occurred').value) el('capture-occurred').value = now.toISOString().slice(0, 16);
    document.querySelectorAll('[name="capture-event"]').forEach(input => input.onchange = updateEventFields);
    updateEventFields(); drawFiles(); updateOutbox(projectId);
    el('capture-photo').onclick = () => el('capture-photo-file').click();
    el('capture-photo-file').onchange = (event) => { addInputFiles(event.target.files); event.target.value = ''; };
    el('capture-audio-file').onchange = (event) => { addInputFiles(event.target.files); event.target.value = ''; };
    el('capture-voice').onclick = toggleRecording;
    el('capture-location').onclick = locate;
    el('capture-extract').onclick = () => interpretEvent(projectId).catch(error => {
      el('capture-extract').disabled = false;
      el('capture-extract').textContent = 'Extract editable event card';
      window.toast(error.message, 'bad');
    });
    el('capture-confirmed').oninput = () => { state.transcriptDirty = true; };
    el('capture-original').oninput = (event) => {
      if (!state.transcriptDirty) el('capture-confirmed').value = event.target.value;
      el('capture-transcript-source').textContent = 'Typed field note—review and confirm below.';
      state.extracted = false;
      for (const id of ['capture-structured-card', 'capture-place-card', 'capture-confirm-card'])
        el(id).hidden = true;
      el('capture-extract').textContent = 'Extract editable event card';
    };
    const applyLanguage = () => {
      const language = el('capture-language').value;
      const rtl = /^(ar|ur|fa|he)(-|$)/i.test(language);
      for (const area of [el('capture-original'), el('capture-confirmed')]) {
        area.lang = language; area.dir = rtl ? 'rtl' : 'auto';
      }
    };
    el('capture-language').onchange = applyLanguage;
    applyLanguage();
    el('capture-activity-search').oninput = (event) => {
      clearTimeout(state.timer); state.timer = setTimeout(() =>
        searchActivities(projectId, event.target.value).catch(() => {}), 260);
    };
    el('capture-save').onclick = () => submit(projectId);
    const flushButton = el('capture-sync-now');
    if (flushButton) flushButton.onclick = () => flush(projectId).then(() => window.render());
  }

  window.addEventListener('online', () => flush(window.S && window.S.project));
  window.addEventListener('offline', () => updateOutbox(window.S && window.S.project));
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', (event) => {
      if (event.data && event.data.type === 'veda-field-sync-complete') {
        updateOutbox(window.S && window.S.project);
        if (window.S && window.S.view === 'capture') window.render();
      }
    });
  }
  window.FieldCapture = {bind, flush, updateOutbox};
})();
