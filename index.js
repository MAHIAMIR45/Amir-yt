const express = require('express');
const fetch = require('node-fetch');
const cors = require('cors');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors({ origin: '*', methods: ['GET', 'OPTIONS'] }));

const BACKEND = "https://yt-amir.onrender.com";

app.get('/', async (req, res) => {
  let youtubeUrl = req.query.url;
  const quality = req.query.quality;   // 1080p, 720p, 360p, 128 etc.

  if (!youtubeUrl) {
    return res.status(400).json({
      success: false,
      error: "YouTube link required! Example: ?url=https://youtu.be/VIDEOID&quality=720p"
    });
  }

  try {
    // Clean URL
    youtubeUrl = youtubeUrl.trim();
    if (!youtubeUrl.startsWith('http')) {
      youtubeUrl = 'https://' + youtubeUrl;
    }

    // Build API URL
    let apiUrl = `\( {BACKEND}/download/video?url= \){encodeURIComponent(youtubeUrl)}`;

    // Agar quality di hai to direct download maango
    if (quality) {
      let height = quality.toLowerCase().replace('p', '').replace('k', '').trim();
      if (height === '128' || height === '48') {
        // Audio ke liye alag endpoint use kar sakte hain
        apiUrl = `\( {BACKEND}/download/audio?url= \){encodeURIComponent(youtubeUrl)}`;
      } else {
        apiUrl += `&height=${height}`;
      }
    }

    const response = await fetch(apiUrl, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
      }
    });

    const contentType = response.headers.get('content-type') || '';

    // Direct File Download (Video ya Audio)
    if (contentType.includes('video') || contentType.includes('audio') || contentType.includes('octet-stream')) {
      const isAudio = contentType.includes('audio') || quality?.includes('128') || quality?.includes('48');
      const ext = isAudio ? 'mp3' : 'mp4';
      const filename = quality ? `download_\( {quality}. \){ext}` : `video.${ext}`;
      
      res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
      return response.body.pipe(res);
    }

    // JSON Response
    const data = await response.json();

    if (data.status === "error") {
      return res.status(400).json({
        success: false,
        error: data.error || "Failed to fetch video"
      });
    }

    // Agar quality di thi aur direct nahi mila to manual match
    if (quality && data.formats?.combined) {
      const q = parseInt(quality);
      const match = data.formats.combined.find(f => f.height === q);
      if (match && match.url) {
        return res.redirect(match.url);
      }
    }

    // Final Response
    res.json({
      success: true,
      developer: { name: "Deadly Dev" },
      video: {
        title: data.title,
        thumbnail: data.thumbnail,
        duration: data.duration,
        channel: data.channel
      },
      formats: data.formats || {},
      note: "Direct download ke liye &quality=1080, 720, 480, 360, 128 use karo"
    });

  } catch (err) {
    res.status(500).json({
      success: false,
      error: "Something went wrong",
      message: err.message
    });
  }
});

app.listen(PORT, () => {
  console.log(`✅ YT Downloader API Running on port ${PORT}`);
});
