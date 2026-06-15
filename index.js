const express = require('express');
const fetch = require('node-fetch');
const cors = require('cors');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors({ origin: '*', methods: ['GET', 'OPTIONS'] }));

const BACKEND = "https://yt-amir.onrender.com";

app.get('/', async (req, res) => {
  let youtubeUrl = req.query.url;
  const quality = req.query.quality;   // 720p, 1080p, 360p, etc.

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

    let apiUrl = `\( {BACKEND}/download/video?url= \){encodeURIComponent(youtubeUrl)}`;

    const response = await fetch(apiUrl, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
      }
    });

    const contentType = response.headers.get('content-type') || '';

    // Direct Video/Audio File (Download)
    if (contentType.includes('video') || contentType.includes('audio')) {
      const filename = quality ? `video_${quality}.mp4` : "video.mp4";
      res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
      return response.body.pipe(res);
    }

    // JSON Response
    const data = await response.json();

    if (data.status === "error") {
      throw new Error(data.error || "Backend error");
    }

    // Quality-based Direct Download
    if (quality && data.formats?.combined) {
      const q = parseInt(quality.replace('p', '').replace('k', ''));
      const match = data.formats.combined.find(f => 
        f.height === q || 
        f.resolution?.includes(q.toString())
      );
      
      if (match && match.url) {
        return res.redirect(match.url);
      }
    }

    // Return Full Data
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
      note: "Direct download: &quality=1080p, 720p, 480p, 360p, 128 etc."
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
  console.log(`✅ API Running on port ${PORT}`);
});
