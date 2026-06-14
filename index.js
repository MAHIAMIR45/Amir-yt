const express = require('express');
const fetch = require('node-fetch');
const cors = require('cors');

const app = express();
const PORT = process.env.PORT || 3000;

// Middleware
app.use(cors({
  origin: '*',
  methods: ['GET', 'OPTIONS']
}));

app.use(express.json());

app.get('/', async (req, res) => {
  const youtubeUrl = req.query.url;
  const requestedQuality = req.query.quality; // Example: 720p, 1080p, 360p etc.

  if (!youtubeUrl) {
    return res.status(400).json({
      success: false,
      error: "YouTube link required! Use: ?url=YOUR_YOUTUBE_LINK",
      example: "?url=https://youtu.be/VIDEOID&quality=720p"
    });
  }

  try {
    // Step 1: Get media items from ytdown.to
    const apiResponse = await fetch('https://app.ytdown.to/proxy.php', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://app.ytdown.to/en34/',
        'Origin': 'https://app.ytdown.to'
      },
      body: new URLSearchParams({ url: youtubeUrl })
    });

    const data = await apiResponse.json();

    if (!data?.api?.mediaItems) {
      throw new Error('No media items found');
    }

    const videoFormats = [];
    const audioFormats = [];

    // Extract Video ID
    let videoId = null;
    if (youtubeUrl.includes('youtu.be/')) {
      videoId = youtubeUrl.split('youtu.be/')[1]?.split('?')[0];
    } else if (youtubeUrl.includes('v=')) {
      videoId = youtubeUrl.split('v=')[1]?.split('&')[0];
    }

    // Process formats
    for (const item of data.api.mediaItems) {
      let quality = 'Unknown';
      let resolution = null;

      if (item.mediaRes && item.mediaRes !== false) {
        resolution = item.mediaRes;
        if (item.mediaRes.includes('x')) {
          const height = parseInt(item.mediaRes.split('x')[1]);
          if (height === 1080) quality = '1080p';
          else if (height === 720) quality = '720p';
          else if (height === 480) quality = '480p';
          else if (height === 360) quality = '360p';
          else if (height === 240) quality = '240p';
          else if (height === 144) quality = '144p';
        }
      }

      if (item.type === 'Audio' && item.mediaQuality) {
        quality = item.mediaQuality;
      }

      // Get real direct download link
      let realDownloadUrl = null;
      let realFileSize = item.mediaFileSize;

      for (let attempt = 0; attempt < 5; attempt++) {
        try {
          const checkRes = await fetch(item.mediaUrl, {
            headers: {
              'User-Agent': 'Mozilla/5.0',
              'Referer': 'https://app.ytdown.to/'
            }
          });

          const contentType = checkRes.headers.get('Content-Type') || '';

          if (contentType.includes('application/json')) {
            const jsonData = await checkRes.json();
            if (jsonData.status === 'completed' && jsonData.fileUrl) {
              realDownloadUrl = jsonData.fileUrl;
              realFileSize = jsonData.fileSize || item.mediaFileSize;
              break;
            }
            await new Promise(r => setTimeout(r, 2000));
          } else {
            realDownloadUrl = item.mediaUrl;
            break;
          }
        } catch (e) {
          await new Promise(r => setTimeout(r, 2000));
        }
      }

      if (realDownloadUrl) {
        const formatData = {
          quality: quality,
          extension: item.mediaExtension || (item.type === 'Audio' ? 'MP3' : 'MP4'),
          size: realFileSize || 'Unknown',
          downloadUrl: realDownloadUrl
        };

        if (item.type !== 'Audio' && resolution) {
          formatData.resolution = resolution;
        }

        if (item.type === 'Audio') {
          audioFormats.push(formatData);
        } else {
          videoFormats.push(formatData);
        }
      }
    }

    // Auto select if quality is requested
    let directDownload = null;
    if (requestedQuality) {
      const selected = [...videoFormats, ...audioFormats]
        .find(f => f.quality.toLowerCase() === requestedQuality.toLowerCase());
      
      if (selected) {
        directDownload = selected.downloadUrl;
      }
    }

    // Video Info
    let realTitle = data.api.title || "Unknown";
    let realDuration = null;
    let realChannel = null;
    let realViews = null;

    if (videoId) {
      try {
        const oembedRes = await fetch(`https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=${videoId}&format=json`);
        const oembedData = await oembedRes.json();
        realTitle = oembedData.title || realTitle;
        realChannel = oembedData.author_name || null;
      } catch (e) {}
    }

    const response = {
      success: true,
      developer: { name: "Deadly Dev" },
      video: {
        title: realTitle,
        thumbnail: `https://img.youtube.com/vi/${videoId}/maxresdefault.jpg`,
        videoId: videoId,
        duration: realDuration || "Unknown",
        channel: realChannel || "Unknown",
        views: realViews || "Not available"
      },
      formats: {
        video: videoFormats,
        audio: audioFormats
      }
    };

    // Agar quality diya gaya ho to direct redirect ya download
    if (directDownload) {
      return res.redirect(directDownload);
    }

    res.json(response);

  } catch (err) {
    res.status(500).json({
      success: false,
      error: err.message || "Failed to process video"
    });
  }
});

app.listen(PORT, () => {
  console.log(`YouTube Downloader API running on port ${PORT}`);
});
