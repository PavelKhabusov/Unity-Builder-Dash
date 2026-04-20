# Widget integration script — adds a WidgetKit app extension target to the
# Unity-exported Xcode project. Invoked by IOSbuild.scpt after Unity export.
#
# Fully env-driven — no hardcoded identity. Values come from ENV vars set
# by IOSbuild.scpt (patched on Install from Unity Builder Dash). Fallbacks
# are placeholder-safe so the script still runs if env is unset.

require 'xcodeproj'

# Work dir resolution order:
#   1. ARGV[0]          explicit override when invoked manually
#   2. ENV['WORK_DIR']  set by IOSbuild.scpt (patched on install)
#   3. script's own dir install_mac_server places this .rb next to IOS/
script_dir   = File.dirname(File.expand_path(__FILE__))
work_dir     = ARGV[0] || ENV['WORK_DIR'] || script_dir
project_path = File.join(work_dir, 'IOS', 'Unity-iPhone.xcodeproj')

# Widget config from env (set by Settings → Install on Mac)
BUNDLE_ID     = ENV['WIDGET_BUNDLE_ID']   || 'com.example.myapp.widget'
TEAM_ID       = ENV['WIDGET_TEAM_ID']     || 'XXXXXXXXXX'
WIDGET_TARGET = ENV['WIDGET_TARGET_NAME'] || 'URLImageWidget'

puts "📂 Using project at #{project_path}"
puts "🔧 Widget: target=#{WIDGET_TARGET}, bundle=#{BUNDLE_ID}, team=#{TEAM_ID}"
project = Xcodeproj::Project.open(project_path)

app_target = project.targets.find { |t| t.name == 'Unity-iPhone' }
abort("❌ Main target not found") unless app_target

# Create the widget target
widget_target = project.new_target(:app_extension, WIDGET_TARGET, :ios, '17.0', nil, :swift)

# Bundle Identifier and Info.plist
plist_path = 'Widgets/Info.plist'
widget_target.build_configurations.each do |config|
  config.build_settings['PRODUCT_BUNDLE_IDENTIFIER'] = BUNDLE_ID
  config.build_settings['INFOPLIST_FILE'] = plist_path
  config.build_settings['CODE_SIGN_ENTITLEMENTS'] = 'Unity-iPhone.entitlements'
  config.build_settings['DEVELOPMENT_TEAM'] = TEAM_ID
  config.build_settings['SWIFT_VERSION'] = '5.0'
  config.build_settings['EMBEDDED_CONTENT_CONTAINS_SWIFT'] = 'YES'
  config.build_settings['LD_RUNPATH_SEARCH_PATHS'] = '$(inherited) @executable_path/Frameworks'
  config.build_settings['PRODUCT_NAME'] = WIDGET_TARGET
  config.build_settings['WRAPPER_EXTENSION'] = 'appex'
  config.build_settings['ENABLE_APPINTENTS_SUGGESTIONS_TRAINING'] = 'NO'
  config.build_settings['ENABLE_APP_SHORTCUTS_FLEXIBLE_MATCHING'] = 'NO'
end

# Add Swift source files to the widget target
group = project.main_group.find_subpath('Widgets/', true)
group.set_source_tree('SOURCE_ROOT')
%W[
  #{WIDGET_TARGET}/ProductData.swift
  #{WIDGET_TARGET}/#{WIDGET_TARGET}.swift
  #{WIDGET_TARGET}/#{WIDGET_TARGET}+Provider.swift
  WidgetBundle.swift
].each do |filename|
  file_path = "Widgets/#{filename}"
  file_ref = group.new_file(file_path)
  build_phase = widget_target.source_build_phase
  build_phase.add_file_reference(file_ref, true)
end

frameworks_phase = widget_target.frameworks_build_phase

frameworks_phase.files.each do |build_file|
    file_ref = build_file.file_ref
    if file_ref && file_ref.path.to_s.downcase.include?('foundation.framework')
      build_file.remove_from_project
      file_ref.remove_from_project
    end
end

%w[
    Foundation
    SwiftUI
    WidgetKit
    Combine
    AppIntents
].each do |framework_name|
  framework_path = "/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk/System/Library/Frameworks/#{framework_name}.framework"

  # Skip if already linked (by filename, not by full path)
  existing_file = project.frameworks_group.files.find do |f|
    File.basename(f.path.to_s) == framework_name
  end

  unless existing_file
    file_ref = project.frameworks_group.new_file(framework_path)
    frameworks_phase.add_file_reference(file_ref, true)
  end
end

copy_phase = app_target.copy_files_build_phases.find { |bp| bp.name == 'Embed App Extensions' }
unless copy_phase
  copy_phase = app_target.new_copy_files_build_phase('Embed App Extensions')
  copy_phase.dst_subfolder_spec = '13'
end
copy_phase.add_file_reference(widget_target.product_reference, true)

project.save
puts "✅ Widget #{WIDGET_TARGET} added to project"
