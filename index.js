const express = require('express');
const fetch = require('node-fetch');
const cors = require('cors');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors({ origin: '*', methods: ['GET', 'OPTIONS'] }));
app.use(express.json());

app.get('/', async (req, res) => {
  const youtubeUrl = req.query.url;
  const requestedQuality = req.query.quality;

  if (!youtubeUrl) {
    return res.status(400).json({
      success: false,
      error: "YouTube link required! Use: ?url=YOUR_LINK&quality=720p"
    });
  }

  try {
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

    if (!data?.api?.mediaItems?.length) {
      throw new Error('No media items found');
    }

    const videoFormats = [];
    const audioFormats = [];

    // Improved Video ID extraction for Shorts + Normal videos
    let videoId = null;
    if (youtubeUrl.includes('youtu.be/')) {
      videoId = youtubeUrl.split('youtu.be/')[1]?.split('?')[0];
    } else if (youtubeUrl.includes('v=')) {
      videoId = youtubeUrl.split('v=')[1]?.split('&')[0];
    } else if (youtubeUrl.includes('/shorts/')) {
      videoId = youtubeUrl.split('/shorts/')[1]?.split('?')[0];
    }

    // Process all formats
    for (const item of data.api.mediaItems) {
      let quality = item.mediaQuality || 'Unknown';
      let resolution = item.mediaRes;

      if (resolution && resolution.includes('x')) {
        const height = parseInt(resolution.split('x')[1]);
        if (height === 1080) quality = '1080p';
        else if (height === 720) quality = '720p';
        else if (height === 480) quality = '480p';
        else if (height === 360) quality = '360p';
        else if (height === 240) quality = '240p';
        else if (height === 144) quality = '144p';
      }

      let realDownloadUrl = null;
      let realFileSize = item.mediaFileSize;

      // Poll for real link
      for (let attempt = 0; attempt < 5; attempt++) {
        try {
          const checkRes = await fetch(item.mediaUrl, {
            headers: { 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://app.ytdown.to/' }
          });

          const contentType = checkRes.headers.get('Content-Type') || '';

          if (contentType.includes('application/json')) {
            const jsonData = await checkRes.json();
            if (jsonData.status === 'completed' && jsonData.fileUrl) {
              realDownloadUrl = jsonData.fileUrl;
              realFileSize = jsonData.fileSize || item.mediaFileSize;
              break;
            }
            await new Promise(r => setTimeout(r, 1500));
          } else {
            realDownloadUrl = item.mediaUrl;
            break;
          }
        } catch (e) {
          await new Promise(r => setTimeout(r, 1500));
        }
      }

      if (realDownloadUrl) {
        const formatData = {
          quality: quality,
          extension: item.mediaExtension || (item.type === 'Audio' ? 'MP3' : 'MP4'),
          size: realFileSize || 'Unknown',
          downloadUrl: realDownloadUrl,
          type: item.type || 'Video'
        };

        if (item.type === 'Audio') {
          audioFormats.push(formatData);
        } else {
          videoFormats.push(formatData);
        }
      }
    }

    // ==================== STRONG QUALITY MATCHING (Fixed for Shorts) ====================
    let directDownload = null;
    if (requestedQuality) {
      const q = requestedQuality.toString().toLowerCase().trim()
                    .replace('p', '').replace('k', '').replace('kbps', '');

      const allFormats = [...videoFormats, ...audioFormats];

      // Multiple matching strategies
      const selected = allFormats.find(f => {
        let fq = f.quality.toString().toLowerCase()
                     .replace('p', '').replace('k', '').replace('kbps', '');
        return fq === q || 
               fq.includes(q) || 
               f.quality.toLowerCase().includes(requestedQuality.toLowerCase());
      });

      if (selected) {
        directDownload = selected.downloadUrl;
      }
    }
    // =================================================================================

    const response = {
      success: true,
      developer: { name: "Deadly Dev" },
      video: {
        title: data.api.title || "Unknown",
        thumbnail: videoId ? `https://img.youtube.com/vi/${videoId}/maxresdefault.jpg` : "",
        videoId: videoId,
        duration: "Unknown",
        channel: "Unknown"
      },
      formats: {
        video: videoFormats,
        audio: audioFormats
      }
    };

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
  console.log(`✅ YouTube Downloader API running on port ${PORT}`);
});
