const express        = require('express');
const fetch          = require('node-fetch');
const cors           = require('cors');
const { spawn }      = require('child_process');

const app      = express();
const PORT     = process.env.PORT || 5000;
const BASE_API = 'https://yt-amir.onrender.com';

app.use(cors({ origin: '*', methods: ['GET', 'OPTIONS'] }));
app.use(express.json());

// ─────────────────────────────────────────────
//  Helpers
// ─────────────────────────────────────────────
function extractVideoId(url) {
  if (!url) return null;
  if (url.includes('youtu.be/'))  return url.split('youtu.be/')[1]?.split('?')[0]?.split('/')[0] || null;
  if (url.includes('/shorts/'))   return url.split('/shorts/')[1]?.split('?')[0]?.split('/')[0] || null;
  if (url.includes('v='))         return url.split('v=')[1]?.split('&')[0]?.split('/')[0] || null;
  return null;
}

function isHlsManifest(url) {
  if (!url) return false;
  return url.includes('manifest.googlevideo') || url.includes('.m3u8');
}

function isDirectMp4(url) {
  if (!url) return false;
  return url.includes('googlevideo.com') && !isHlsManifest(url);
}

function parseHeight(q) {
  if (!q) return null;
  const n = parseInt(q.toString().toLowerCase().replace('p', '').trim());
  return (!isNaN(n) && n > 0) ? n : null;
}

function isAudioQuality(q) {
  if (!q) return false;
  const s = q.toString().toLowerCase().trim();
  if (['audio', 'mp3', 'm4a', 'webm', 'opus'].includes(s)) return true;
  if (/^\d+$/.test(s) && parseInt(s) <= 320) return true;
  return false;
}

function nearestHeight(heights, req) {
  if (!heights.length) return null;
  if (heights.includes(req)) return req;
  const lower  = heights.filter(h => h <= req).sort((a, b) => b - a);
  const higher = heights.filter(h => h >  req).sort((a, b) => a - b);
  return lower.length ? lower[0] : higher[0];
}

// Build best-per-height map; DIRECT beats HLS for same height
function buildBestMap(formats) {
  const map = new Map();
  for (const f of formats) {
    if (!f.height || !f.url) continue;
    const existing = map.get(f.height);
    if (!existing) {
      map.set(f.height, f);
    } else if (!isDirectMp4(existing.url) && isDirectMp4(f.url)) {
      map.set(f.height, f);
    }
  }
  return map;
}

// ─────────────────────────────────────────────
//  Stream HLS → fragmented MP4 via ffmpeg
//  HLS combined streams already have video+audio
// ─────────────────────────────────────────────
function streamHls(hlsUrl, filename, res) {
  res.setHeader('Content-Type',        'video/mp4');
  res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
  res.setHeader('Transfer-Encoding',   'chunked');
  res.setHeader('X-Accel-Buffering',   'no');

  const ff = spawn('ffmpeg', [
    '-y',
    '-i',         hlsUrl,
    '-c:v',       'copy',
    '-c:a',       'copy',
    '-bsf:a',     'aac_adtstoasc',
    '-movflags',  'frag_keyframe+empty_moov+default_base_moof',
    '-f',         'mp4',
    'pipe:1'
  ], { stdio: ['ignore', 'pipe', 'pipe'] });

  ff.stdout.pipe(res);
  ff.stderr.on('data', () => {});   // suppress ffmpeg logs

  ff.on('error', (err) => {
    if (!res.headersSent)
      res.status(500).json({ success: false, error: 'ffmpeg error: ' + err.message });
  });

  ff.on('close', () => {
    if (!res.writableEnded) res.end();
  });

  // Kill ffmpeg if client disconnects
  res.on('close',   () => { try { ff.kill('SIGKILL'); } catch(e){} });
  res.on('finish',  () => { try { ff.kill('SIGKILL'); } catch(e){} });
}

// ─────────────────────────────────────────────────────────────────────────────
//  Main route
//  GET /?url=LINK               → JSON (same structure as old API)
//  GET /?url=LINK&quality=720p  → video+audio MP4 stream via ffmpeg (HLS)
//                                 or redirect for itag-18 direct formats
//  GET /?url=LINK&quality=128   → redirect to best audio URL
// ─────────────────────────────────────────────────────────────────────────────
app.get('/', async (req, res) => {
  const youtubeUrl       = req.query.url;
  const requestedQuality = req.query.quality;

  if (!youtubeUrl) {
    return res.status(400).json({
      success: false,
      error: 'YouTube link required! Use: ?url=YOUR_LINK&quality=360p'
    });
  }

  try {
    // Fetch video + audio info in parallel
    const [vRes, aRes] = await Promise.all([
      fetch(`${BASE_API}/download/video?url=${encodeURIComponent(youtubeUrl)}`),
      fetch(`${BASE_API}/download/audio?url=${encodeURIComponent(youtubeUrl)}`)
    ]);
    const [vData, aData] = await Promise.all([vRes.json(), aRes.json()]);

    if (vData.status !== 'ok') throw new Error(vData.error || 'Video info failed');

    const videoId   = extractVideoId(youtubeUrl);
    const combined  = vData.formats?.combined   || [];
    const videoOnly = vData.formats?.video_only || [];
    const audioFmts = (aData.all_audio_formats  || []).filter(f => f.url);

    // ── Build per-height format maps ───────────────────────────────────────
    // HLS combined (video+audio together in one stream) — accessible from server
    const hlsMap = new Map();
    for (const f of combined) {
      if (!f.height || !isHlsManifest(f.url)) continue;
      if (!hlsMap.has(f.height)) hlsMap.set(f.height, f);
    }

    // Direct combined (itag 18 etc) — video+audio, redirect to phone
    const directCombinedMap = new Map();
    for (const f of combined) {
      if (!f.height || !isDirectMp4(f.url)) continue;
      if (!directCombinedMap.has(f.height)) directCombinedMap.set(f.height, f);
    }

    // Video-only direct (no audio) — phone can access, server cannot
    const voMap = buildBestMap(videoOnly.filter(f => isDirectMp4(f.url)));

    // All available heights (any source)
    const allHeights = [...new Set([
      ...hlsMap.keys(),
      ...directCombinedMap.keys(),
      ...voMap.keys()
    ])].sort((a, b) => b - a);

    // Sort audio: highest bitrate first, prefer m4a
    const sortedAudio = [...audioFmts].sort((a, b) => {
      const diff = (b.abr || 0) - (a.abr || 0);
      if (diff !== 0) return diff;
      return a.ext === 'm4a' ? -1 : 1;
    });

    // ── AUDIO quality ──────────────────────────────────────────────────────
    if (isAudioQuality(requestedQuality)) {
      const reqKbps = parseInt(requestedQuality) || 9999;
      const picked  = [...sortedAudio].sort((a, b) =>
        Math.abs((a.abr || 0) - reqKbps) - Math.abs((b.abr || 0) - reqKbps)
      )[0];
      if (!picked) return res.status(404).json({ success: false, error: 'Audio nahi mila' });
      return res.redirect(picked.url);
    }

    // ── VIDEO quality requested ─────────────────────────────────────────────
    if (requestedQuality) {
      const reqH = parseHeight(requestedQuality);
      if (!reqH) return res.status(400).json({ success: false, error: 'Invalid quality: ' + requestedQuality });

      if (!allHeights.length)
        return res.status(404).json({ success: false, error: 'Koi format nahi mila' });

      const pickedH = nearestHeight(allHeights, reqH);
      const title   = (vData.title || 'video').replace(/[^\w\s-]/g, '').trim().replace(/\s+/g, '_').slice(0, 50);

      // Priority 1: HLS combined → ffmpeg stream (video+audio ✅)
      if (hlsMap.has(pickedH)) {
        return streamHls(hlsMap.get(pickedH).url, `${title}_${pickedH}p.mp4`, res);
      }

      // Priority 2: Direct combined (itag 18) → redirect (video+audio ✅)
      if (directCombinedMap.has(pickedH)) {
        return res.redirect(directCombinedMap.get(pickedH).url);
      }

      // Priority 3: Video-only direct → redirect (phone can access, no audio ⚠️)
      if (voMap.has(pickedH)) {
        return res.redirect(voMap.get(pickedH).url);
      }

      return res.status(404).json({
        success: false,
        error: `${reqH}p nahi mila`,
        available: allHeights.map(h => h + 'p')
      });
    }

    // ── No quality → full JSON (same structure as old API) ─────────────────
    const myBase = `${req.protocol}://${req.get('host')}`;

    // Determine audio availability for each height
    const videoFormats = allHeights.map(h => {
      const hasAudio = hlsMap.has(h) || directCombinedMap.has(h);
      const f = hlsMap.get(h) || directCombinedMap.get(h) || voMap.get(h);
      return {
        quality:     h + 'p',
        extension:   'MP4',
        size:        f?.filesize_human || 'Unknown',
        hasAudio,
        downloadUrl: `${myBase}/?url=${encodeURIComponent(youtubeUrl)}&quality=${h}p`
      };
    });

    const audioFormats = sortedAudio.map(f => ({
      quality:     Math.round(f.abr || 0).toString(),
      extension:   (f.ext || 'webm').toUpperCase(),
      size:        f.filesize_human || 'Unknown',
      downloadUrl: f.url
    }));

    return res.json({
      success:   true,
      developer: { name: 'Deadly Dev' },
      video: {
        title:     vData.title     || 'Unknown',
        thumbnail: videoId
          ? `https://img.youtube.com/vi/${videoId}/maxresdefault.jpg`
          : (vData.thumbnail || ''),
        videoId:   videoId
      },
      formats: {
        video: videoFormats,
        audio: audioFormats
      }
    });

  } catch (err) {
    return res.status(500).json({ success: false, error: err.message || 'Server error' });
  }
});

app.listen(PORT, () => {
  console.log(`✅ API running on port ${PORT}`);
});
