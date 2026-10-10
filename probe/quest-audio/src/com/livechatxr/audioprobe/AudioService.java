package com.livechatxr.audioprobe;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.os.Handler;
import android.os.IBinder;
import android.os.SystemClock;
import android.util.Log;

public class AudioService extends Service {
    public static final String START = "com.livechatxr.audioprobe.START";
    public static final String STOP = "com.livechatxr.audioprobe.STOP";
    private static final String CHANNEL = "audio-probe";
    private final Handler handler = new Handler();
    private AudioManager audio;
    private AudioFocusRequest focus;
    private MediaPlayer player;
    private long started, audible, playingSince;
    private boolean playing;
    private final Runnable tick = new Runnable() {
        @Override public void run() {
            log("tick");
            handler.postDelayed(this, 30000);
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        started = SystemClock.elapsedRealtime();
        audio = (AudioManager) getSystemService(AUDIO_SERVICE);
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(
                new NotificationChannel(CHANNEL, "Audio probe", NotificationManager.IMPORTANCE_LOW));
        log("create");
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null || !START.equals(intent.getAction())) {
            cleanup();
            stopSelfResult(startId);
            return START_NOT_STICKY;
        }
        if (player != null) {
            log("duplicate Start ignored");
            return START_NOT_STICKY;
        }
        try {
            PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                    PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, AudioService.class).setAction(STOP),
                    PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            startForeground(1, new Notification.Builder(this, CHANNEL)
                    .setSmallIcon(android.R.drawable.ic_media_play).setContentTitle("Quest Audio Probe")
                    .setContentText("Numbered speech test · Stop to end").setOngoing(true)
                    .setContentIntent(open).addAction(android.R.drawable.ic_media_pause, "Stop", stop).build());
            AudioAttributes attributes = new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build();
            focus = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                    .setAudioAttributes(attributes).setWillPauseWhenDucked(true)
                    .setOnAudioFocusChangeListener(this::onFocus, handler).build();
            int result = audio.requestAudioFocus(focus);
            log("focus request result=" + result);
            if (result != AudioManager.AUDIOFOCUS_REQUEST_GRANTED) {
                cleanup();
                stopSelfResult(startId);
                return START_NOT_STICKY;
            }
            player = MediaPlayer.create(this, R.raw.speech, attributes, 0);
            if (player == null) throw new IllegalStateException("media unavailable");
            player.setLooping(true);
            player.setOnErrorListener((p, what, extra) -> {
                log("media error=" + what + "/" + extra);
                cleanup();
                stopSelf();
                return true;
            });
            resume();
            handler.postDelayed(tick, 30000);
        } catch (RuntimeException e) {
            log("start error=" + e.getClass().getSimpleName());
            cleanup();
            stopSelfResult(startId);
        }
        return START_NOT_STICKY;
    }

    private void onFocus(int change) {
        log("focus change=" + change);
        if (player == null) return;
        try {
            if (change == AudioManager.AUDIOFOCUS_GAIN) resume();
            else if (change == AudioManager.AUDIOFOCUS_LOSS) {
                cleanup();
                stopSelf();
            } else if (change == AudioManager.AUDIOFOCUS_LOSS_TRANSIENT
                    || change == AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK) {
                if (playing) player.pause();
                endInterval();
                log("paused for focus");
            }
        } catch (RuntimeException e) {
            log("focus error=" + e.getClass().getSimpleName());
            cleanup();
            stopSelf();
        }
    }

    private void resume() {
        if (playing) return;
        player.start();
        playingSince = SystemClock.elapsedRealtime();
        playing = true;
        log("playing");
    }

    private void endInterval() {
        if (playing) audible += SystemClock.elapsedRealtime() - playingSince;
        playing = false;
    }

    private void cleanup() {
        handler.removeCallbacks(tick);
        endInterval();
        if (player != null) {
            player.release();
            player = null;
        }
        if (focus != null) {
            AudioFocusRequest old = focus;
            focus = null;
            audio.abandonAudioFocusRequest(old);
        }
        stopForeground(true);
        log("cleanup");
    }

    private void log(String event) {
        long now = SystemClock.elapsedRealtime();
        Log.i("QuestAudioProbe", event + " elapsed_ms=" + (now - started)
                + " playback_ms=" + (audible + (playing ? now - playingSince : 0)));
    }

    @Override public void onDestroy() {
        cleanup();
        log("destroy");
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
