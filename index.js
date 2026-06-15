const express = require('express');
const fetch = require('node-fetch');
const cors = require('cors');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors({ origin: '*', methods: ['GET', 'OPTIONS'] }));

const BACKEND = "https://yt-amir.onrender.com";

app.get('/', async (req, res) => {
  let youtubeUrl = req.query.url;
  const quality = req.query.quality;

  if (!youtubeUrl) {
    return res.status(400).json({
      success: false,
      error: "YouTube link missing! Use: ?url=FULL_YOUTUBE_LINK&quality=720p"
    });
  }

  try {
    // Clean & Fix URL
    youtubeUrl = youtubeUrl.trim();
    if (youtubeUrl.startsWith('youtu.be')) {
      youtubeUrl = 'https://' + youtubeUrl;
    }
    if (!youtubeUrl.startsWith('http')) {
      youtubeUrl = 'https://' + youtubeUrl;
    }

    let apiUrl = `\( {BACKEND}/download/video?url= \){encodeURIComponent(youtubeUrl)}`;

    const response = await fetch(apiUrl, {
      headers: { 'User-Agent': 'Mozilla/5.0' }
    });

    const contentType = response.headers.get('content-type') || '';

    // Direct Download (Video/Audio)
    if (contentType.includes('video') || contentType.includes('audio')) {
      const ext = contentType.includes('audio') ? 'mp3' : 'mp4';
      const filename = quality ? `video_\( {quality}. \){ext}` : `video.${ext}`;
      res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
      return response.body.pipe(res);
    }

    const data = await response.json();

    if (data.status === "error") {
      return res.status(400).json({
        success: false,
        error: data.error || "Backend error"
      });
    }

    // Direct Quality Download
    if (quality && data.formats?.combined?.length) {
      const q = parseInt(quality.replace(/[^0-9]/g, ''));
      const match = data.formats.combined.find(f => 
        (f.height && f.height === q) || 
        f.resolution?.toLowerCase().includes(q.toString())
      );
      
      if (match && match.url) {
        return res.redirect(match.url);
      }
    }

    // Full Response
    res.json({
      success: true,
      video: {
        title: data.title,
        thumbnail: data.thumbnail,
        duration: data.duration,
        channel: data.channel
      },
      formats: data.formats,
      note: "Direct Download: Add &quality=720p, 1080p, 480p, 360p, 128"
    });

  } catch (err) {
    res.status(500).json({
      success: false,
      error: "Failed to process",
      message: err.message
    });
  }
});

app.listen(PORT, () => {
  console.log(`✅ API is Running on port ${PORT}`);
});
