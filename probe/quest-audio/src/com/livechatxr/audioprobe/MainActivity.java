package com.livechatxr.audioprobe;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.widget.Toast;

public class MainActivity extends Activity {
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        setContentView(R.layout.main);
        findViewById(R.id.start).setOnClickListener(v -> {
            try {
                startForegroundService(new Intent(this, AudioService.class).setAction(AudioService.START));
                Toast.makeText(this, "Start requested. Listen for numbered speech.", Toast.LENGTH_SHORT).show();
            } catch (RuntimeException e) {
                Toast.makeText(this, "Start unavailable. Return to this app and retry.", Toast.LENGTH_LONG).show();
            }
        });
        findViewById(R.id.stop).setOnClickListener(v -> {
            stopService(new Intent(this, AudioService.class));
            Toast.makeText(this, "Stop requested.", Toast.LENGTH_SHORT).show();
        });
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
    }
}
