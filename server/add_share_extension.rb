# Share extension integration — adds a Share (com.apple.share-services) app
# extension target to the Unity-exported Xcode project. Sources travel inside
# the export (iOS/ShareExtension/, copied by the Unity post-processor). Skips
# silently when that folder is absent, so projects without an extension are
# unaffected. Invoked by ios_build.scpt right after add_widget_dependency.rb.
#
# Env: SHARE_TARGET_NAME (default KartotekaShare), SHARE_BUNDLE_ID (default
# <app bundle id>.share), WIDGET_TEAM_ID (signing team), APP_GROUP_ID.

require 'xcodeproj'
require 'fileutils'

script_dir   = File.dirname(File.expand_path(__FILE__))
work_dir     = ARGV[0] || ENV['WORK_DIR'] || script_dir
ios_dir      = File.join(work_dir, 'iOS')
project_path = File.join(ios_dir, 'Unity-iPhone.xcodeproj')
src_dir      = File.join(ios_dir, 'ShareExtension')

unless File.directory?(src_dir)
  puts "ℹ️  No ShareExtension/ in export — share extension skipped"
  exit 0
end

TARGET   = ENV['SHARE_TARGET_NAME'] || 'KartotekaShare'
TEAM_ID  = ENV['WIDGET_TEAM_ID'] || 'XXXXXXXXXX'
APP_GROUP = ENV['APP_GROUP_ID'].to_s

project = Xcodeproj::Project.open(project_path)
app_target = project.targets.find { |t| t.name == 'Unity-iPhone' }
abort("❌ Main target not found") unless app_target

app_bundle = app_target.build_configurations.first.build_settings['PRODUCT_BUNDLE_IDENTIFIER'].to_s
BUNDLE_ID = ENV['SHARE_BUNDLE_ID'].to_s.empty? ? "#{app_bundle}.share" : ENV['SHARE_BUNDLE_ID']
puts "🔧 Share: target=#{TARGET}, bundle=#{BUNDLE_ID}, team=#{TEAM_ID}, group=#{APP_GROUP}"

project.targets.select { |t| t.name == TARGET }.each do |old|
  app_target.copy_files_build_phases.each do |phase|
    phase.files.select { |bf| bf.file_ref == old.product_reference }.each(&:remove_from_project)
  end
  old.remove_from_project
end

plist_path = File.join(src_dir, 'Info.plist')
main_plist = Xcodeproj::Plist.read_from_path(File.join(ios_dir, 'Info.plist')) rescue {}
plist = Xcodeproj::Plist.read_from_path(plist_path)
plist['CFBundleVersion'] = main_plist['CFBundleVersion'] || '1'
plist['CFBundleShortVersionString'] = main_plist['CFBundleShortVersionString'] || '1.0'
Xcodeproj::Plist.write_to_path(plist, plist_path)

ent_path = File.join(src_dir, "#{TARGET}.entitlements")
unless APP_GROUP.empty?
  ent = File.exist?(ent_path) ? Xcodeproj::Plist.read_from_path(ent_path) : {}
  ent['com.apple.security.application-groups'] = [APP_GROUP]
  Xcodeproj::Plist.write_to_path(ent, ent_path)
end

target = project.new_target(:app_extension, TARGET, :ios, '15.0', nil, :swift)
target.build_configurations.each do |config|
  s = config.build_settings
  s['PRODUCT_BUNDLE_IDENTIFIER'] = BUNDLE_ID
  s['INFOPLIST_FILE'] = 'ShareExtension/Info.plist'
  s['CODE_SIGN_ENTITLEMENTS'] = "ShareExtension/#{TARGET}.entitlements"
  s['CODE_SIGN_STYLE'] = 'Automatic'
  s['DEVELOPMENT_TEAM'] = TEAM_ID
  s['SWIFT_VERSION'] = '5.0'
  s['IPHONEOS_DEPLOYMENT_TARGET'] = '15.0'
  s['TARGETED_DEVICE_FAMILY'] = '1,2'
  s['PRODUCT_NAME'] = TARGET
  s['WRAPPER_EXTENSION'] = 'appex'
  s['APPLICATION_EXTENSION_API_ONLY'] = 'YES'
  s['LD_RUNPATH_SEARCH_PATHS'] = '$(inherited) @executable_path/Frameworks @executable_path/../../Frameworks'
  s['ALWAYS_SEARCH_USER_PATHS'] = 'NO'
  s['ALWAYS_EMBED_SWIFT_STANDARD_LIBRARIES'] = 'NO'
  s['ENABLE_BITCODE'] = 'NO'
  s['GENERATE_INFOPLIST_FILE'] = 'NO'
  s['ENABLE_APPINTENTS_SUGGESTIONS_TRAINING'] = 'NO'
  s['ENABLE_APP_SHORTCUTS_FLEXIBLE_MATCHING'] = 'NO'
end

group = project.main_group.find_subpath('ShareExtension', true)
group.set_source_tree('SOURCE_ROOT')
group.set_path('ShareExtension')
Dir.glob(File.join(src_dir, '*.swift')).sort.each do |f|
  ref = group.new_file(File.basename(f))
  target.source_build_phase.add_file_reference(ref, true)
end
group.new_file('Info.plist')
group.new_file("#{TARGET}.entitlements")

target.frameworks_build_phase.files.each do |bf|
  next unless bf.file_ref && bf.file_ref.path.to_s.downcase.include?('foundation.framework')
  bf.remove_from_project
end
%w[SwiftUI UIKit UniformTypeIdentifiers AVFoundation ImageIO Security].each do |name|
  target.add_system_framework(name)
end

app_target.add_dependency(target)
copy_phase = app_target.copy_files_build_phases.find { |bp| bp.name == 'Embed App Extensions' }
unless copy_phase
  copy_phase = app_target.new_copy_files_build_phase('Embed App Extensions')
  copy_phase.dst_subfolder_spec = '13'
end
bf = copy_phase.add_file_reference(target.product_reference, true)
bf.settings = { 'ATTRIBUTES' => ['RemoveHeadersOnCopy'] }

attrs = project.root_object.attributes['TargetAttributes'] ||= {}
attrs[target.uuid] = { 'DevelopmentTeam' => TEAM_ID, 'ProvisioningStyle' => 'Automatic' }

project.save
puts "✅ Share extension #{TARGET} added to project"
