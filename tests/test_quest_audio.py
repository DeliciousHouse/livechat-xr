"""Compile the real Service against tiny JVM doubles; no Android runtime claims."""
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "probe" / "quest-audio"
A = "{http://schemas.android.com/apk/res/android}"
# These doubles cover only the calls used by the probe, not Android OS policy.
STUBS = {
    "android/content/Intent.java": 'package android.content; public class Intent { String action; public Intent() {} public Intent(Object c, Class<?> t) {} public Intent setAction(String a) {action=a;return this;} public String getAction(){return action;} }',
    "android/app/Service.java": '''package android.app; public class Service {
 public static final int START_NOT_STICKY=2; public static final String AUDIO_SERVICE="audio", NOTIFICATION_SERVICE="notification";
 public int foreground, removed, stopped; public Object getSystemService(String s){return s.equals(AUDIO_SERVICE)?new android.media.AudioManager():new NotificationManager();}
 public void onCreate(){} public int onStartCommand(android.content.Intent i,int f,int id){return 0;} public void onDestroy(){}
 public android.os.IBinder onBind(android.content.Intent i){return null;} public void startForeground(int id,Notification n){foreground++;}
 public void stopForeground(boolean b){removed++;} public boolean stopSelfResult(int id){stopped++;return true;} public void stopSelf(){stopped++;}
}''',
    "android/app/Notification.java": '''package android.app; public class Notification { public static class Builder {
 public Builder(Object c,String s){} public Builder setSmallIcon(int i){return this;} public Builder setContentTitle(String s){return this;}
 public Builder setContentText(String s){return this;} public Builder setOngoing(boolean b){return this;}
 public Builder setContentIntent(PendingIntent p){return this;} public Builder addAction(int i,String s,PendingIntent p){return this;}
 public Notification build(){return new Notification();} }}''',
    "android/app/NotificationChannel.java": 'package android.app; public class NotificationChannel {public NotificationChannel(String i,String n,int p){} }',
    "android/app/NotificationManager.java": 'package android.app; public class NotificationManager {public static final int IMPORTANCE_LOW=2; public void createNotificationChannel(NotificationChannel c){} }',
    "android/app/PendingIntent.java": 'package android.app; public class PendingIntent {public static final int FLAG_IMMUTABLE=1, FLAG_UPDATE_CURRENT=2; public static PendingIntent getActivity(Object c,int r,android.content.Intent i,int f){return new PendingIntent();} public static PendingIntent getService(Object c,int r,android.content.Intent i,int f){return new PendingIntent();} }',
    "android/os/IBinder.java": 'package android.os; public interface IBinder {}',
    "android/os/SystemClock.java": 'package android.os; public class SystemClock {public static long elapsedRealtime(){return 1000;} }',
    "android/os/Handler.java": 'package android.os; public class Handler {public boolean postDelayed(Runnable r,long d){return true;} public void removeCallbacks(Runnable r){} }',
    "android/util/Log.java": 'package android.util; public class Log {public static int i(String t,String m){return 0;} }',
    "android/R.java": 'package android; public class R {public static class drawable {public static final int ic_media_play=1,ic_media_pause=2;} }',
    "android/media/AudioAttributes.java": 'package android.media; public class AudioAttributes {public static final int USAGE_MEDIA=1,CONTENT_TYPE_SPEECH=1; public static class Builder {public Builder setUsage(int i){return this;} public Builder setContentType(int i){return this;} public AudioAttributes build(){return new AudioAttributes();}} }',
    "android/media/AudioFocusRequest.java": '''package android.media; public class AudioFocusRequest {public static class Builder {
 public Builder(int i){} public Builder setAudioAttributes(AudioAttributes a){return this;} public Builder setWillPauseWhenDucked(boolean b){return this;}
 public Builder setOnAudioFocusChangeListener(AudioManager.OnAudioFocusChangeListener l,android.os.Handler h){AudioManager.listener=l;return this;}
 public AudioFocusRequest build(){return new AudioFocusRequest();} }}''',
    "android/media/AudioManager.java": '''package android.media; public class AudioManager {
 public static final int AUDIOFOCUS_GAIN=1,AUDIOFOCUS_LOSS=-1,AUDIOFOCUS_LOSS_TRANSIENT=-2,AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK=-3,AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK=3,AUDIOFOCUS_REQUEST_GRANTED=1;
 public static int result=1,requests,abandons; public static OnAudioFocusChangeListener listener;
 public interface OnAudioFocusChangeListener {void onAudioFocusChange(int c);}
 public int requestAudioFocus(AudioFocusRequest r){requests++;return result;} public int abandonAudioFocusRequest(AudioFocusRequest r){abandons++;return 1;} }''',
    "android/media/MediaPlayer.java": '''package android.media; public class MediaPlayer {
 public static int created,released; public static boolean fail; public static MediaPlayer last; public boolean playing,loop;
 public interface OnErrorListener {boolean onError(MediaPlayer p,int w,int e);} public OnErrorListener error;
 public static MediaPlayer create(Object c,int r,AudioAttributes a,int s){if(fail)return null;created++;last=new MediaPlayer();return last;}
 public void setLooping(boolean b){loop=b;} public void setOnErrorListener(OnErrorListener l){error=l;}
 public void start(){playing=true;} public void pause(){playing=false;} public void release(){released++;playing=false;} }''',
    "com/livechatxr/audioprobe/R.java": 'package com.livechatxr.audioprobe; public class R {public static class raw {public static final int speech=1;} }',
    "com/livechatxr/audioprobe/MainActivity.java": 'package com.livechatxr.audioprobe; public class MainActivity {}',
    "com/livechatxr/audioprobe/Contract.java": '''package com.livechatxr.audioprobe;
import android.content.Intent; import android.media.*;
public class Contract {
 static void check(boolean b){if(!b)throw new AssertionError();}
 static AudioService service(){AudioService s=new AudioService();s.onCreate();return s;}
 static void start(AudioService s){check(s.onStartCommand(new Intent().setAction(AudioService.START),0,1)==2);}
 public static void main(String[] args){
 AudioService s=service();start(s);start(s);check(MediaPlayer.created==1 && AudioManager.requests==1 && MediaPlayer.last.loop);
 AudioManager.listener.onAudioFocusChange(AudioManager.AUDIOFOCUS_LOSS_TRANSIENT);check(!MediaPlayer.last.playing);
 start(s);check(!MediaPlayer.last.playing && MediaPlayer.created==1);
 AudioManager.listener.onAudioFocusChange(AudioManager.AUDIOFOCUS_GAIN);check(MediaPlayer.last.playing);
 AudioManager.listener.onAudioFocusChange(AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK);check(!MediaPlayer.last.playing);
 s.onStartCommand(new Intent().setAction(AudioService.STOP),0,2);s.onDestroy();
 check(MediaPlayer.released==1 && AudioManager.abandons==1 && s.removed>0 && s.stopped>0);
 // A queued gain after Stop must not resurrect audio.
 AudioManager.listener.onAudioFocusChange(AudioManager.AUDIOFOCUS_GAIN);check(!MediaPlayer.last.playing);
 s=service();start(s);AudioManager.listener.onAudioFocusChange(AudioManager.AUDIOFOCUS_LOSS);s.onDestroy();check(MediaPlayer.released==2);
 s=service();start(s);check(MediaPlayer.last.error.onError(MediaPlayer.last,1,2));s.onDestroy();check(MediaPlayer.released==3);
 s=service();start(s);s.onDestroy();check(MediaPlayer.released==4 && AudioManager.abandons==4 && s.removed>0);
 AudioManager.result=0;s=service();start(s);s.onDestroy();check(MediaPlayer.created==4 && s.stopped>0 && s.removed>0);
 AudioManager.result=1;MediaPlayer.fail=true;s=service();start(s);s.onDestroy();check(MediaPlayer.created==4 && s.stopped>0);
 s=service();s.onStartCommand(null,0,1);check(s.stopped>0);
 System.out.println("Service boundary: duplicate Start, pause/gain, Stop/destroy, loss, error, denial, null intent PASS");
 }}''',
}


class QuestAudio(unittest.TestCase):
    def test_service_contract(self):
        manifest = ET.parse(ROOT / "AndroidManifest.xml").getroot()
        self.assertEqual(manifest.get("package"), "com.livechatxr.audioprobe")
        sdk = manifest.find("uses-sdk")
        assert sdk is not None
        self.assertEqual(sdk.get(A + "targetSdkVersion"), "34")
        self.assertEqual({p.get(A + "name") for p in manifest.findall("uses-permission")}, {
            "android.permission.FOREGROUND_SERVICE", "android.permission.FOREGROUND_SERVICE_MEDIA_PLAYBACK",
            "android.permission.POST_NOTIFICATIONS"})
        service = manifest.find("application/service")
        assert service is not None
        self.assertEqual(service.get(A + "foregroundServiceType"), "mediaPlayback")
        self.assertEqual(service.get(A + "exported"), "false")
        activity = manifest.find("application/activity")
        assert activity is not None
        self.assertEqual(activity.get(A + "exported"), "true")
        category = activity.find("intent-filter/category")
        assert category is not None
        self.assertEqual(category.get(A + "name"),
                         "android.intent.category.LAUNCHER")
        with wave.open(str(ROOT / "res/raw/speech.wav")) as audio:
            self.assertEqual(audio.getnchannels(), 1)
            self.assertGreater(audio.getnframes() / audio.getframerate(), 10)
        dev = Path(os.environ.get("LOCALAPPDATA", "")) / "android-dev"
        # Set QUEST_AUDIO_APK for the local built-artifact boundary, not a source-only check.
        if os.environ.get("QUEST_AUDIO_APK"):
            apk = Path(os.environ["QUEST_AUDIO_APK"])
            bt = dev / "sdk/build-tools/34.0.0"
            badging = subprocess.check_output([str(bt / "aapt2.exe"), "dump", "badging", str(apk)], text=True)
            self.assertIn("package: name='com.livechatxr.audioprobe'", badging)
            self.assertIn("targetSdkVersion:'34'", badging)
            self.assertIn("launchable-activity: name='com.livechatxr.audioprobe.MainActivity'", badging)
            tree = subprocess.check_output([str(bt / "aapt2.exe"), "dump", "xmltree", str(apk),
                                            "--file", "AndroidManifest.xml"], text=True)
            service_tree = tree.split("E: service", 1)[1]
            self.assertIn('".AudioService"', service_tree)
            self.assertIn("exported(0x01010010)=false", service_tree)
            self.assertIn("foregroundServiceType(0x01010599)=0x00000002", service_tree)
            for permission in manifest.findall("uses-permission"):
                self.assertIn(permission.get(A + "name"), badging)
            with zipfile.ZipFile(apk) as archive:
                self.assertEqual(archive.getinfo("res/raw/speech.wav").compress_type, zipfile.ZIP_STORED,
                                 "MediaPlayer.create needs an uncompressed raw-resource file descriptor")
                self.assertEqual(archive.read("res/raw/speech.wav"), (ROOT / "res/raw/speech.wav").read_bytes())
                dex = archive.read("classes.dex")
                self.assertTrue(dex.startswith(b"dex\n"))
                for name in (b"Lcom/livechatxr/audioprobe/AudioService;", b"Lcom/livechatxr/audioprobe/MainActivity;"):
                    self.assertIn(name, dex)
            print("APK boundary: package, launcher, permissions, media service, exact audio, dex PASS")
        jdks = sorted(dev.glob("jdk-17*/bin/javac.exe"))
        javac = str(jdks[0]) if jdks else shutil.which("javac")
        if not javac:
            self.skipTest("JDK unavailable: JVM Service boundary not exercised")
        java = str(Path(javac).with_name("java.exe" if javac.endswith(".exe") else "java"))
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sources = []
            for name, text in STUBS.items():
                p = tmp / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
                sources.append(str(p))
            subprocess.run([javac, "-encoding", "UTF-8", "-d", str(tmp), *sources,
                            str(ROOT / "src/com/livechatxr/audioprobe/AudioService.java")], check=True)
            subprocess.run([java, "-cp", str(tmp), "com.livechatxr.audioprobe.Contract"], check=True)


if __name__ == "__main__":
    unittest.main()
