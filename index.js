const express = require('express');
const fetch = require('node-fetch');
const cors = require('cors');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors({ origin: '*', methods: ['GET', 'OPTIONS'] }));

const BACKEND = "https://yt-amir.onrender.com";

app.get('/', async (req, res) => {
  let youtubeUrl = req.query.url;
  const quality = req.query.quality;   // 1080, 720, 360, 128 etc.

  if (!youtubeUrl) {
    return res.status(400).json({
      success: false,
      error: "URL missing! Example: ?url=https://youtube.com/shorts/VIDEO&quality=720"
    });
  }

  try {
    youtubeUrl = youtubeUrl.trim();
    if (!youtubeUrl.startsWith('http')) {
      youtubeUrl = 'https://' + youtubeUrl;
    }

    let apiUrl = `\( {BACKEND}/download/video?url= \){encodeURIComponent(youtubeUrl)}`;

    const response = await fetch(apiUrl, {
      headers: { 'User-Agent': 'Mozilla/5.0' }
    });

    const contentType = response.headers.get('content-type') || '';

    // Agar direct file aa raha hai
    if (contentType.includes('video') || contentType.includes('audio')) {
      const filename = `video${quality ? '_' + quality : ''}.mp4`;
      res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
      return response.body.pipe(res);
    }

    const data = await response.json();

    if (data.status === "error") {
      return res.status(400).json({ success: false, error: data.error });
    }

    // ==================== QUALITY MATCHING ====================
    let directUrl = null;

    if (quality) {
      const q = quality.toString().toLowerCase().replace('p', '').replace('k', '').trim();
      const qNum = parseInt(q);

      // Combined Video + Audio
      if (data.formats?.combined?.length) {
        directUrl = data.formats.combined.find(f => 
          f.height == qNum || 
          f.resolution?.includes(q) ||
          f.format_note?.includes(q)
        )?.url;
      }

      // Agar audio quality chahiye
      if (!directUrl && (qNum === 128 || qNum === 251 || q === 'audio')) {
        if (data.formats?.audio_only?.length) {
          directUrl = data.formats.audio_only[0]?.url;   // best audio
        }
      }
    }

    // Direct Redirect (Best Case)
    if (directUrl) {
      return res.redirect(directUrl);
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
      note: "Use &quality=1080, 720, 480, 360, 128 for direct download"
    });

  } catch (err) {
    res.status(500).json({
      success: false,
      error: "Failed to process request",
      message: err.message
    });
  }
});

app.listen(PORT, () => {
  console.log(`✅ API Running Successfully on port ${PORT}`);
});
