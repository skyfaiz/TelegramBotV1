# Changelog

## [Unreleased] - TTS & Voice Clone Feature Branch

### Added

#### Text-to-Speech (AWS Polly Integration)
- **New audio input method**: Users can now choose between uploading audio, using TTS, or voice cloning (placeholder) after uploading an image
- **TTS endpoint** (`/api/tts`): Backend endpoint using AWS Polly with three engine types:
  - Standard (free)
  - Neural (2⭐ per 100 characters)
  - Generative (5⭐ per 100 characters)
- **Voice preview**: Users can listen to a sample of each voice before selecting
- **Generated audio delivery**: TTS audio is sent to users so they can hear it before proceeding
- **Voice options**: Multiple voices available per engine (Joanna, Matthew, Ruth, Stephen, Danielle, Gregory)

#### Voice Cloning (Placeholder)
- **Clone endpoint** (`/api/clone`): Placeholder endpoint returning 501 Not Implemented
- **UI flow**: Shows "Coming Soon" message and redirects to other audio methods

#### Pricing
- TTS costs are now itemized separately in the order summary
- Cost breakdown shows video generation + AI voice costs when applicable

### Changed

#### User-Facing Messages
- Removed all technical infrastructure details (no mention of RunPod, S3, Job IDs)
- Simplified error messages with just a reference ID for support
- Updated video caption to remove technical model references
- Changed "Job submitted to RunPod" → "Generation started!"
- Error messages now clearly state refund eligibility

#### Bot Flow
- After image upload, users see three audio input options via inline keyboard
- TTS flow: Enter text → Choose engine → Preview/Select voice → Audio generated & sent → Continue to prompt
- Updated help message with TTS pricing information

### Configuration
- Added AWS Polly credentials to `config.py`:
  - `AWS_ACCESS_KEY_ID`
  - `AWS_SECRET_ACCESS_KEY`
  - `AWS_REGION`
- Updated `.env.example` with AWS credential placeholders

### Files Modified
- `bot.py` - New conversation states, handlers, TTS flow, voice preview
- `routes/infinitetalk.py` - TTS and Clone API endpoints
- `config.py` - AWS credentials
- `.env.example` - AWS credential placeholders
- `.gitignore` - Exclude all .md files except README.md
