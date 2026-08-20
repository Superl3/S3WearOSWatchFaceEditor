plugins { alias(libs.plugins.android.application) }

android {
    namespace = "com.photo2wff.template"
    compileSdk = 34
    defaultConfig {
        applicationId = "com.photo2wff.template"
        minSdk = 33
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }
}
