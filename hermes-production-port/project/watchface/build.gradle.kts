plugins { alias(libs.plugins.android.application) }

android {
    namespace = "com.photo2wff.watchface"
    compileSdk = 34
    defaultConfig {
        applicationId = "com.photo2wff.watchface"
        minSdk = 33
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }
    buildTypes {
        debug { isMinifyEnabled = true }
        release { isMinifyEnabled = true; isShrinkResources = false; signingConfig = signingConfigs.getByName("debug") }
    }
}
