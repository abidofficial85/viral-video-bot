/* Viral Video Mini App — vanilla JS, no build step.
 *
 * Flow: pick a video -> show Monetag Rewarded Interstitial
 * (show_<ZONE_ID>() from the Monetag SDK tag) -> the promise resolving
 * means the ad was fully watched, so it unlocks the video ->
 * POST /api/unlock (Telegram initData proves the real user) ->
 * 1-hour token -> <video src="/api/stream?token=...">.
 *
 * Home page: a 3-column poster grid (Reels-tab style) — poster image
 * from /api/thumb when the video has a Telegram thumbnail, otherwise
 * a dark fallback tile; view counts shown compactly (396K / 2.9M).
 *
 * Stream failure: if the stream errors or delivers no data at all,
 * the player shows a Bengali failure message plus an
 * 'আবার চেষ্টা করুন' button that retries the SAME stream with the
 * SAME 1-hour unlock token (no new ad) and reloads the poster.
 *
 * TEST MODE (no Monetag zone configured on the server): the ad step
 * is a short fake countdown that always counts as 'valued', so the
 * whole flow can be tested end to end before Monetag is set up.
 */
(function () {
  'use strict';

  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) { try { tg.ready(); tg.expand(); } catch (e) { /* older clients */ } }

  var config = { testMode: true, zoneId: null, sdkUrl: null };
  var videos = [];
  var selected = null;
  var monetagLoaded = false;
  var streamToken = null;    // token from the last unlock, reusable for 1 hour
  var loadTimer = null;      // watchdog for a stream that never delivers data
  var streamStarted = false; // true once the current attempt delivers data
  var retryBtn = null;       // created at init, shown only on stream failure

  function $(id) { return document.getElementById(id); }

  function fmtDuration(s) {
    s = parseInt(s, 10) || 0;
    if (!s) return '';
    return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
  }

  function fmtViews(n) {
    n = parseInt(n, 10) || 0;
    var trim1 = function (x) { return x.toFixed(1).replace(/\.0$/, ''); };
    if (n >= 1000000) return trim1(n / 1000000) + 'M';
    if (n >= 1000) return trim1(n / 1000) + 'K';
    return String(n);
  }

  function setStatus(text) { $('status').textContent = text || ''; }
  function setGateStatus(text) { $('gateStatus').textContent = text || ''; }

  function loadMonetagSdk() {
    if (!config.sdkUrl || !config.zoneId || monetagLoaded) return;
    var script = document.createElement('script');
    script.src = config.sdkUrl;
    script.async = true;
    // Mirror the Monetag tag exactly: the SDK binds show_<zone> to the
    // zone named in these attributes — without them the function that
    // showRealAd calls would never be defined.
    script.dataset.zone = config.zoneId;
    script.dataset.sdk = 'show_' + config.zoneId;
    script.onload = function () { monetagLoaded = true; };
    document.head.appendChild(script);
  }

  function fakeAd() {
    // Test mode only: 3-second simulated ad, always 'valued'.
    return new Promise(function (resolve) {
      var n = 3;
      setGateStatus('টেস্ট এড চলছে... ' + n);
      var timer = setInterval(function () {
        n -= 1;
        if (n <= 0) {
          clearInterval(timer);
          resolve({ reward_event_type: 'valued' });
        } else {
          setGateStatus('টেস্ট এড চলছে... ' + n);
        }
      }, 1000);
    });
  }

  function showRealAd() {
    var fnName = 'show_' + config.zoneId;
    var fn = window[fnName];
    if (typeof fn !== 'function') {
      return Promise.reject(new Error(
        'Monetag SDK লোড হয়নি। Zone ID / SDK URL ঠিক আছে কিনা দেখুন।'));
    }
    // Exactly Monetag's Rewarded Interstitial snippet: show_<zone>()
    // with no arguments; the promise resolving IS the reward (ad fully
    // watched). Some SDK builds resolve with {reward_event_type}; an
    // explicit non-valued result still counts as not rewarded, and a
    // rejection (ad closed early / failed) propagates as an error.
    return fn().then(function (res) {
      if (res && res.reward_event_type &&
          res.reward_event_type !== 'valued') {
        throw new Error('এডটি পুরো দেখা হয়নি (valued হয়নি)। আবার চেষ্টা করুন।');
      }
      return { reward_event_type: 'valued' };
    });
  }

  function requestId() {
    var userId = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user &&
      tg.initDataUnsafe.user.id) || 'web';
    return 'u' + userId + '_v' + (selected && selected.id) + '_' +
      Date.now() + '_' + Math.floor(Math.random() * 1e6);
  }

  function unlockVideo() {
    var ymid = requestId();
    var adPromise = config.testMode ? fakeAd() : showRealAd();
    $('adBtn').disabled = true;
    setGateStatus('এড লোড হচ্ছে...');
    return adPromise.then(function (res) {
      var valued = res && res.reward_event_type === 'valued';
      if (!valued) {
        throw new Error('এডটি পুরো দেখা হয়নি (valued হয়নি)। আবার চেষ্টা করুন।');
      }
      setGateStatus('এড শেষ! ভিডিও আনলক হচ্ছে...');
      if (!tg || !tg.initData) {
        throw new Error('Telegram পরিচয় পাওয়া যায়নি। Mini App-টি Telegram-এর ভেতর থেকে খুলুন।');
      }
      return fetch('/api/unlock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          initData: tg.initData,
          videoId: selected.id,
          rewardEventType: res.reward_event_type,
          ymid: ymid
        })
      }).then(function (r) { return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || 'আনলক হয়নি।');
        return j.token;
      }); });
    });
  }

  function openGate(video) {
    selected = video;
    $('gateTitle').textContent = '🎬 ' + video.title;
    $('gateText').textContent = 'এড দেখলেই ভিডিওটি আনলক হবে।';
    $('adBtn').disabled = false;
    setGateStatus('');
    $('gate').classList.remove('hidden');
  }

  function closeGate() { $('gate').classList.add('hidden'); }

  function clearLoadTimer() {
    if (loadTimer) { clearTimeout(loadTimer); loadTimer = null; }
  }

  function hideStreamError() {
    if (retryBtn) retryBtn.classList.add('hidden');
  }

  // Shown ONLY when the stream itself fails (error event, or a load
  // that delivers no data at all). Never shown for a normal pause or
  // while the video is still loading: those states never reach here.
  function showStreamError() {
    clearLoadTimer();
    setStatus('ভিডিও লোড হয়নি — সার্ভারে সাময়িক সমস্যা হয়েছে।');
    if (retryBtn) retryBtn.classList.remove('hidden');
  }

  function playerPosterUrl(bustCache) {
    if (!selected || !selected.thumb) return null;
    var url = '/api/thumb?video=' + encodeURIComponent(selected.id) +
      '&v=' + (selected.thumbVer || 0);
    return bustCache ? url + '&r=' + Date.now() : url;
  }

  function playVideo(token) {
    // The token stays reusable for 1 hour, so a failed stream can be
    // retried with this same token — no new ad, no new unlock.
    streamToken = token;
    closeGate();
    $('listSection').classList.add('hidden');
    $('playerSection').classList.remove('hidden');
    $('playerTitle').textContent = '🎬 ' + selected.title;
    startStream(token, false);
  }

  function startStream(token, isRetry) {
    var player = $('player');
    hideStreamError();
    clearLoadTimer();
    streamStarted = false;
    // On a retry, force the poster to refetch (cache-busted) so a
    // poster that failed with the stream is not stuck on the broken
    // image; on first play the plain thumb URL is used, as before.
    var poster = playerPosterUrl(isRetry);
    if (poster) { player.poster = poster; }
    else { player.removeAttribute('poster'); }
    player.src = '/api/stream?token=' + encodeURIComponent(token);
    setStatus('ভিডিও লোড হচ্ছে...');
    player.onplaying = function () {
      streamStarted = true;
      clearLoadTimer();
      hideStreamError();
      setStatus('');
    };
    player.onloadeddata = function () {
      streamStarted = true;
      clearLoadTimer();
    };
    player.onerror = function () {
      // Guard: ignore errors once we have left the player (the back
      // button resets the element) — those are not stream failures.
      if (!streamToken || $('playerSection').classList.contains('hidden')) return;
      showStreamError();
    };
    // Backstop for a load that hangs with no data at all (e.g. an
    // upstream 502 that never surfaces as an error): 30s with zero
    // data and still at 0:00 counts as a failure. If data arrives
    // later, onplaying clears this UI again on its own.
    loadTimer = setTimeout(function () {
      if (!streamToken || $('playerSection').classList.contains('hidden')) return;
      if (!streamStarted && player.readyState < 2 && player.currentTime === 0) {
        showStreamError();
      }
    }, 30000);
    var p = player.play();
    if (p && p.catch) p.catch(function () {
      // A failed load rejects play() too, and the 'error' handler owns
      // that case — never let this overwrite the failure message with
      // 'Play চাপ দিন।' (that misleading state was the old bug: the
      // player sat at 0:00 telling the user to press Play).
      if (player.error || (retryBtn && !retryBtn.classList.contains('hidden'))) return;
      setStatus('Play চাপ দিন।');
    });
  }

  function renderList() {
    var list = $('videoList');
    list.innerHTML = '';
    $('emptyMsg').classList.toggle('hidden', videos.length > 0);
    videos.forEach(function (v) {
      var tile = document.createElement('div');
      tile.className = 'tile';
      var dur = fmtDuration(v.duration);
      var poster = v.thumb
        ? '<img class="poster-img" src="/api/thumb?video=' +
          encodeURIComponent(v.id) + '&v=' + (v.thumbVer || 0) +
          '" loading="lazy" alt="">'
        : '<div class="poster-fallback">▶</div>';
      tile.innerHTML =
        '<div class="poster">' + poster + '</div>' +
        (dur ? '<div class="tile-dur">' + dur + '</div>' : '') +
        '<div class="tile-info">' +
          '<div class="tile-title">' + escapeHtml(v.title) + '</div>' +
          '<div class="tile-views">👁 ' + fmtViews(v.views) + '</div>' +
        '</div>';
      tile.addEventListener('click', function () { openGate(v); });
      list.appendChild(tile);
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function preselectFromStartParam() {
    var param = tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param;
    if (param && param.indexOf('video_') === 0) {
      var id = param.slice(6);
      var found = null;
      videos.forEach(function (v) { if (v.id === id) found = v; });
      if (found) openGate(found);
    }
  }

  // Retry button for a failed stream: same token, no new ad, no new
  // unlock. Created here so index.html and style.css stay untouched;
  // it reuses the existing gold button style and starts hidden.
  retryBtn = document.createElement('button');
  retryBtn.id = 'retryBtn';
  retryBtn.className = 'btn gold hidden';
  retryBtn.textContent = 'আবার চেষ্টা করুন';
  retryBtn.addEventListener('click', function () {
    if (!streamToken) return;
    var player = $('player');
    player.pause();
    player.removeAttribute('src');
    player.load();
    startStream(streamToken, true);
  });
  $('status').insertAdjacentElement('afterend', retryBtn);

  $('adBtn').addEventListener('click', function () {
    unlockVideo().then(playVideo).catch(function (err) {
      setGateStatus(err.message || 'কিছু একটা ভুল হয়েছে।');
      $('adBtn').disabled = false;
    });
  });
  $('gateClose').addEventListener('click', closeGate);
  $('backBtn').addEventListener('click', function () {
    streamToken = null;
    streamStarted = false;
    clearLoadTimer();
    hideStreamError();
    var player = $('player');
    player.pause();
    player.removeAttribute('src');
    player.load();
    $('playerSection').classList.add('hidden');
    $('listSection').classList.remove('hidden');
    setStatus('');
  });

  fetch('/api/config').then(function (r) { return r.json(); }).then(function (c) {
    config = c;
    if (c.testMode) $('testBadge').classList.remove('hidden');
    loadMonetagSdk();
    return fetch('/api/videos').then(function (r) { return r.json(); });
  }).then(function (j) {
    videos = j.videos || [];
    renderList();
    preselectFromStartParam();
  }).catch(function () {
    $('emptyMsg').textContent = 'সার্ভারের সাথে যোগাযোগ হয়নি। আবার চেষ্টা করুন।';
    $('emptyMsg').classList.remove('hidden');
  });
})();
