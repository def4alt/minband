import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.minband.android"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.minband.android"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1"
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    packaging {
        // The Rust core is built with 16 KB page alignment (tools/build-android.sh); keep it
        // uncompressed so the loader can map it directly.
        jniLibs { useLegacyPackaging = false }
    }

    // The detector models (assets/*.onnx, *.tflite) are plain files; never compress them so ORT /
    // TFLite can mmap them.
    androidResources { noCompress += listOf("onnx", "tflite") }
}

kotlin {
    compilerOptions { jvmTarget.set(JvmTarget.JVM_17) }
}

dependencies {
    implementation("com.google.ar:core:1.49.0")
    // uniffi Kotlin bindings (app/src/main/java/dev/minband/core, generated) need JNA.
    implementation("net.java.dev.jna:jna:5.15.0@aar")
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.20.0")
    // TFLite + GPU delegate: the detector's fast path on phones whose GPU runs the graph (the
    // MediaTek NNAPI driver cannot). TfliteDetector uses these; YoloOnnxDetector is the fallback.
    implementation("org.tensorflow:tensorflow-lite:2.16.1")
    implementation("org.tensorflow:tensorflow-lite-gpu:2.16.1")
    implementation("org.tensorflow:tensorflow-lite-gpu-api:2.16.1")

    testImplementation("junit:junit:4.13.2")
}
