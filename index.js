const express = require('express');
const fetch = require('node-fetch');
const cors = require('cors');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors({ origin: '*', methods: ['GET', 'OPTIONS'] }));

const BACKEND = "https://yt-amir.onrender.com";

app.get('/', async (req, res) => {
  const youtubeUrl = req.query.url;
  let quality = req.query.quality;   // e.g. 720p, 1080p, 360p, 128 etc.

  if (!youtubeUrl) {
    return res.status(400).json({
      success: false,
      error: "YouTube link required → ?url=YOUTUBE_LINK&quality=720p"
    });
  }

  try {
    let apiUrl = `\( {BACKEND}/download/video?url= \){encodeURIComponent(youtubeUrl)}`;

    // Agar quality di gayi hai to direct merged video maango
    if (quality) {
      const height = quality.toLowerCase().replace('p', '').replace('k', '');
      apiUrl += `&height=${height}`;
    }

    const response = await fetch(apiUrl, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
      }
    });

    const contentType = response.headers.get('content-type') || '';

    // Agar direct video file aa raha hai (merged)
    if (contentType.includes('video') || contentType.includes('audio')) {
      res.setHeader('Content-Disposition', `attachment; filename="video.mp4"`);
      return response.body.pipe(res);
    }

    // Agar JSON aa raha hai (formats list)
    const data = await response.json();

    // Quality matching agar direct nahi mila
    if (quality && data.formats?.combined) {
      const q = parseInt(quality);
      const match = data.formats.combined.find(f => f.height === q);
      if (match && match.url) {
        return res.redirect(match.url);
      }
    }

    // Return full info
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
      note: "Use &quality=720p, 1080p, 480p, 360p etc. for direct download"
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
  console.log(`🚀 API Running on port ${PORT} | Backend: ${BACKEND}`);
});
